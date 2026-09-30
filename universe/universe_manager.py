"""
Universe Manager — 5-state ticker lifecycle state machine.

Manages the lifecycle of all tickers in the Arconian trading universe,
from initial screening through active signal generation to removal.

States: CANDIDATE → OBSERVATION → ACTIVE → SUSPENDED → REMOVED

Key rules:
  - OBSERVATION: passes all 6 filters but < 120 trading days of history.
                 Data is collected; signals are computed but NOT traded.
  - ACTIVE:      passes all 6 filters AND ≥ 120 days of history.
                 Full signal scoring and trade generation enabled.
  - SUSPENDED:   same filter has failed for ≥ 5 consecutive trading days.
                 New signals paused; existing positions managed normally.
  - REMOVED:     suspended ≥ 30 calendar days, or manually removed, or delisted.

Hysteresis (prevents thrashing):
  - ACTIVE/OBSERVATION → SUSPENDED: same filter fails for 5 consecutive days
  - SUSPENDED → ACTIVE/OBSERVATION: all filters pass for 3 consecutive days

Methods:
  run_monthly_refresh(candidate_symbols)  — screen and add new tickers
  run_daily_check()                       — run filter checks, apply transitions
  get_active_universe()                   — tickers in ACTIVE state
  get_observation_universe()              — tickers in OBSERVATION state
  suspend_ticker(ticker, reason)          — manual suspension
  remove_ticker(ticker, reason)           — manual removal

Supplements reference: Doc 3, Sections 3.1 and 3.2
"""

import logging
from datetime import date, datetime, timedelta
from timeutils import utcnow
from typing import Optional

import pandas as pd

from config.config_loader import ConfigLoader
from db import session_scope
from models import Override, UniverseState
from universe.delay_score import compute_all_delay_scores
from universe.universe_state import FilterCheckResult, TickerState

logger = logging.getLogger(__name__)

# Days in SUSPENDED before automatic transition to REMOVED
_SUSPENSION_TO_REMOVED_DAYS = 30
# Consecutive fails before SUSPENDED
_FAIL_DAYS_TO_SUSPEND = 5
# Consecutive passes (while SUSPENDED) before re-ACTIVE/OBSERVATION
_PASS_DAYS_TO_REACTIVATE = 3
# Days of history to transition OBSERVATION → ACTIVE
_MIN_DAYS_FOR_ACTIVE = 120
# Minimum days of history to enter the universe at all
_MIN_DAYS_FOR_CANDIDATE = 60
# Recompute delay_score when more than this many days old (or NULL)
_DELAY_SCORE_STALE_DAYS = 25


def _today_utc() -> date:
    """Current date in UTC.

    Why: ``run_daily_check`` derives ``today`` from ``utcnow()``, and
    durations like (today - suspension_start).days are computed against that
    UTC reference. Anything writing into ``suspension_start`` /
    ``delay_score_as_of`` must use the same clock or rows misclassify around
    local-vs-UTC midnight crossings (e.g. 8 PM ET on Apr 30 = May 1 UTC).
    """
    return utcnow().date()


class UniverseManager:
    """
    Manages the Arconian ticker universe state machine.

    Args:
        config: Loaded ConfigLoader instance.
        yf_adapter: YFinanceAdapter for market data.
        schwab_adapter: SchwabAdapter for live quotes/options (optional —
            gracefully skips spread/options checks if None).
    """

    def __init__(
        self,
        config: ConfigLoader,
        yf_adapter,          # YFinanceAdapter
        schwab_adapter=None, # SchwabAdapter | None
    ) -> None:
        self._config = config
        self._yf = yf_adapter
        self._schwab = schwab_adapter
        self._u = config.universe  # shorthand
        logger.info("UniverseManager initialised")

    # ------------------------------------------------------------------
    # Monthly refresh
    # ------------------------------------------------------------------

    def run_monthly_refresh(self, candidate_symbols: list[str]) -> dict[str, str]:
        """
        Screen a list of candidate symbols and add qualifying tickers to the
        universe state machine.

        Existing tickers are not re-screened here — they go through
        run_daily_check(). Tickers in REMOVED state may re-enter as CANDIDATE
        if they now pass filters.

        Args:
            candidate_symbols: Ticker symbols to screen. In production, this
                comes from a market-cap-screener API call. In Phase 0/1, pass
                a manually curated list.

        Returns:
            Dict mapping ticker → action taken ('added_observation', 'added_active',
            'skipped_failed_filters', 'skipped_already_tracked').
        """
        logger.info("Monthly refresh: screening %d candidates", len(candidate_symbols))
        outcomes: dict[str, str] = {}
        now = utcnow()

        with session_scope() as session:
            # Load existing states to avoid re-adding tracked tickers
            existing = {
                r.ticker: r.state
                for r in session.query(UniverseState).all()
            }
            # Check overrides
            blocked = {
                o.ticker
                for o in session.query(Override)
                .filter(Override.active.is_(True), Override.override_type == "block")
                .all()
            }

            for ticker in candidate_symbols:
                if ticker in blocked:
                    logger.debug("Monthly refresh: %s blocked by override", ticker)
                    outcomes[ticker] = "skipped_blocked"
                    continue

                current_state = existing.get(ticker)
                if current_state in (
                    TickerState.OBSERVATION,
                    TickerState.ACTIVE,
                    TickerState.SUSPENDED,
                ):
                    # Already tracked and healthy — don't reset their state
                    outcomes[ticker] = "skipped_already_tracked"
                    continue

                # Run filter checks (but we don't have real-time data for midday/spread
                # during the monthly refresh which may run outside market hours)
                result = self._run_filter_checks(ticker, use_intraday=False)
                history_days = self._estimate_history_days(ticker, result)

                if not result.all_pass:
                    logger.debug(
                        "Monthly refresh: %s failed filters (%s)",
                        ticker, result.failure_summary,
                    )
                    outcomes[ticker] = "skipped_failed_filters"
                    # If it was REMOVED, keep it REMOVED — it'll re-enter naturally
                    continue

                # Determine initial state based on history length
                if history_days is not None and history_days >= _MIN_DAYS_FOR_ACTIVE:
                    new_state = TickerState.ACTIVE
                    outcomes[ticker] = "added_active"
                elif history_days is not None and history_days >= _MIN_DAYS_FOR_CANDIDATE:
                    new_state = TickerState.OBSERVATION
                    outcomes[ticker] = "added_observation"
                else:
                    logger.debug(
                        "Monthly refresh: %s insufficient history (%s days)",
                        ticker, history_days,
                    )
                    outcomes[ticker] = "skipped_insufficient_history"
                    continue

                if current_state == TickerState.REMOVED:
                    # Re-entering — update existing record
                    record = session.get(UniverseState, ticker)
                    if record:
                        record.state = new_state.value
                        record.state_since = now
                        record.consecutive_fail_days = 0
                        record.consecutive_pass_days = 0
                        record.suspension_reason = None
                        record.suspension_start = None
                        record.history_days = history_days
                        record.last_checked = now
                else:
                    # Brand new ticker
                    record = UniverseState(
                        ticker=ticker,
                        state=new_state.value,
                        state_since=now,
                        history_days=history_days,
                        consecutive_fail_days=0,
                        consecutive_pass_days=0,
                        last_checked=now,
                    )
                    session.add(record)

                logger.info(
                    "Monthly refresh: %s → %s (history=%s days)",
                    ticker, new_state.value, history_days,
                )

        logger.info(
            "Monthly refresh complete: %d processed, %d added",
            len(candidate_symbols),
            sum(1 for v in outcomes.values() if v.startswith("added")),
        )

        # Refresh delay_score for the full active+observation set on monthly cadence.
        try:
            self.recompute_delay_scores(only_stale=False)
        except Exception:
            logger.exception("recompute_delay_scores (post monthly-refresh) failed; continuing")

        return outcomes

    # ------------------------------------------------------------------
    # Daily check
    # ------------------------------------------------------------------

    def run_daily_check(self) -> dict[str, str]:
        """
        Run filter checks for all ACTIVE, OBSERVATION, and SUSPENDED tickers.
        Apply state transitions based on results and hysteresis rules.

        Should run at ~8:00 AM ET (pre-market, per spec).

        Returns:
            Dict mapping ticker → outcome ('passed', 'suspended', 'removed',
            'reactivated', 'transitioned_to_active').
        """
        logger.info("Daily universe check starting")
        outcomes: dict[str, str] = {}
        now = utcnow()
        today = now.date()

        # 1. Snapshot the working set in a short read transaction, then release
        #    the connection before any network I/O. Previously the whole loop —
        #    including per-ticker yfinance/Schwab calls — ran inside one open
        #    write transaction, which (a) held a multi-minute SQLite write lock
        #    against the live scheduler and (b) let a single raising ticker roll
        #    back the entire day's state-machine progress (F-14).
        with session_scope() as session:
            tickers = [
                r.ticker
                for r in session.query(UniverseState.ticker)
                .filter(
                    UniverseState.state.in_([
                        TickerState.ACTIVE.value,
                        TickerState.OBSERVATION.value,
                        TickerState.SUSPENDED.value,
                    ])
                )
                .all()
            ]

        for ticker in tickers:
            try:
                # 2. Fetch filter data OUTSIDE any DB transaction.
                result = self._run_filter_checks(ticker, use_intraday=True)
                history_days = self._estimate_history_days(ticker, result)

                # 3. Apply the transition in a short per-ticker write transaction.
                with session_scope() as session:
                    record = session.get(UniverseState, ticker)
                    if record is None:
                        # Concurrently removed between snapshot and now.
                        continue
                    outcome = self._process_ticker(
                        record, TickerState(record.state),
                        result, history_days, today, now,
                    )
                    record.last_checked = now
                outcomes[ticker] = outcome
            except Exception:
                # Per-ticker isolation (F-14): one bad ticker must not abort the
                # whole daily check or undo other tickers' committed transitions.
                logger.exception(
                    "Daily check failed for %s; leaving its state unchanged", ticker
                )
                outcomes[ticker] = "error"

        logger.info(
            "Daily check complete: %d tickers — %s",
            len(outcomes),
            {k: sum(1 for v in outcomes.values() if v == k)
             for k in set(outcomes.values())},
        )

        # Recompute delay_score for any ticker whose value is NULL or stale.
        # delay_score is monthly-cadence (Hou-Moskowitz uses 52+ weeks of weekly
        # returns) so most days this is a no-op fast path. Auto-bootstraps
        # newly-added tickers and recovers from any prior gap in coverage.
        try:
            self.recompute_delay_scores(only_stale=True)
        except Exception:
            logger.exception("recompute_delay_scores (post daily-check) failed; continuing")

        return outcomes

    # ------------------------------------------------------------------
    # Delay score recompute
    # ------------------------------------------------------------------

    def recompute_delay_scores(
        self,
        only_stale: bool = True,
        stale_after_days: int = _DELAY_SCORE_STALE_DAYS,
    ) -> dict[str, Optional[float]]:
        """
        Recompute Hou-Moskowitz price delay scores for ACTIVE/OBSERVATION tickers.

        The score requires ≥52 weeks of weekly returns and is by design a
        monthly-cadence feature (whitepaper §3.2.5, §7.4). The result is
        persisted to ``universe_state.delay_score`` and the as-of date to
        ``universe_state.delay_score_as_of`` so the signal engine can read
        it at scan time and stamp ``signal_log.delay_score_as_of_signal``.

        Args:
            only_stale: If True (default), only recompute for tickers whose
                stored value is NULL or older than ``stale_after_days``.
                If False, recompute for every ACTIVE/OBSERVATION ticker.
            stale_after_days: Threshold in days for the staleness check.

        Returns:
            Dict mapping ticker → newly computed score (or None if data was
            insufficient). Empty if nothing needed recomputation.
        """
        today = _today_utc()
        cutoff = today - timedelta(days=stale_after_days)

        with session_scope() as session:
            records = (
                session.query(UniverseState)
                .filter(
                    UniverseState.state.in_([
                        TickerState.ACTIVE.value,
                        TickerState.OBSERVATION.value,
                    ])
                )
                .all()
            )
            if only_stale:
                tickers_to_compute = [
                    r.ticker for r in records
                    if r.delay_score is None
                    or r.delay_score_as_of is None
                    or r.delay_score_as_of < cutoff
                ]
            else:
                tickers_to_compute = [r.ticker for r in records]

        if not tickers_to_compute:
            logger.debug("recompute_delay_scores: nothing stale, skipping")
            return {}

        logger.info(
            "recompute_delay_scores: computing for %d tickers (only_stale=%s)",
            len(tickers_to_compute), only_stale,
        )
        scores = compute_all_delay_scores(tickers_to_compute, self._yf)

        # Persist in a separate write transaction
        n_written = 0
        with session_scope() as session:
            for ticker, score in scores.items():
                record = session.get(UniverseState, ticker)
                if record is None:
                    continue
                # Only overwrite the as-of date when we have a real score —
                # otherwise leave the prior value untouched so subsequent
                # daily runs keep retrying rather than burning the stale window.
                if score is not None:
                    record.delay_score = float(score)
                    record.delay_score_as_of = today
                    n_written += 1

        logger.info(
            "recompute_delay_scores: persisted %d / %d (others had insufficient data)",
            n_written, len(tickers_to_compute),
        )
        return scores

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------

    def get_active_universe(self) -> list[str]:
        """Return list of tickers currently in ACTIVE state."""
        with session_scope() as session:
            return [
                r.ticker for r in session.query(UniverseState.ticker)
                .filter(UniverseState.state == TickerState.ACTIVE.value)
                .all()
            ]

    def get_observation_universe(self) -> list[str]:
        """Return list of tickers currently in OBSERVATION state."""
        with session_scope() as session:
            return [
                r.ticker for r in session.query(UniverseState.ticker)
                .filter(UniverseState.state == TickerState.OBSERVATION.value)
                .all()
            ]

    def get_universe_summary(self) -> dict[str, list[str]]:
        """Return all tickers grouped by state."""
        with session_scope() as session:
            records = session.query(UniverseState).all()
        summary: dict[str, list[str]] = {s.value: [] for s in TickerState}
        for r in records:
            summary[r.state].append(r.ticker)
        return summary

    # ------------------------------------------------------------------
    # Manual operations
    # ------------------------------------------------------------------

    def suspend_ticker(self, ticker: str, reason: str) -> bool:
        """
        Manually suspend a ticker (bypasses hysteresis).

        Args:
            ticker: Ticker symbol.
            reason: Human-readable reason for suspension.

        Returns:
            True if the ticker was found and suspended, False otherwise.
        """
        with session_scope() as session:
            record = session.get(UniverseState, ticker)
            if record is None:
                logger.warning("suspend_ticker: %s not found in universe", ticker)
                return False
            record.state = TickerState.SUSPENDED.value
            record.state_since = utcnow()
            record.suspension_reason = reason
            record.suspension_start = _today_utc()
            record.consecutive_fail_days = _FAIL_DAYS_TO_SUSPEND  # treat as if threshold was hit
            record.consecutive_pass_days = 0
        logger.info("Manual suspension: %s (%s)", ticker, reason)
        return True

    def remove_ticker(self, ticker: str, reason: str) -> bool:
        """
        Manually remove a ticker from the universe (immediate, no hysteresis).

        Also creates an active Override entry so the ticker is not re-added
        during the next monthly refresh.

        Args:
            ticker: Ticker symbol.
            reason: Human-readable reason for removal.

        Returns:
            True if found and removed, False otherwise.
        """
        with session_scope() as session:
            record = session.get(UniverseState, ticker)
            if record is None:
                logger.warning("remove_ticker: %s not found in universe", ticker)
                return False
            record.state = TickerState.REMOVED.value
            record.state_since = utcnow()
            record.suspension_reason = reason

            # Add a permanent override block
            override = Override(
                ticker=ticker,
                override_type="block",
                reason=f"Manually removed: {reason}",
                created_by="manual",
                active=True,
            )
            session.add(override)
        logger.info("Manual removal: %s (%s)", ticker, reason)
        return True

    # ------------------------------------------------------------------
    # Internal — state transition logic
    # ------------------------------------------------------------------

    def _process_ticker(
        self,
        record: UniverseState,
        state: TickerState,
        result: FilterCheckResult,
        history_days: Optional[int],
        today: date,
        now: datetime,
    ) -> str:
        """
        Apply one daily step of the state machine for a single ticker.

        ``result``/``history_days`` are fetched by the caller (outside the DB
        transaction, see ``run_daily_check``); this method performs no network
        I/O — it only mutates ``record`` based on the precomputed filter result.
        """

        # ---- SUSPENDED: check for automatic REMOVED transition first ----
        # This is purely date-based, so it still applies on a data-outage day.
        if state == TickerState.SUSPENDED:
            if record.suspension_start is not None:
                days_suspended = (today - record.suspension_start).days
                if days_suspended >= _SUSPENSION_TO_REMOVED_DAYS:
                    record.state = TickerState.REMOVED.value
                    record.state_since = now
                    logger.info(
                        "%s → REMOVED (suspended %d days)", record.ticker, days_suspended
                    )
                    return "removed"

        # ---- Total data outage → inconclusive: change NO counters (F-14) ----
        # An all-None result is not a pass. Counting it as one would reset fail
        # streaks and advance reactivation hysteresis on days we learned nothing.
        if result.is_inconclusive:
            logger.warning(
                "%s: all filter checks inconclusive (data outage) — "
                "leaving counters and state unchanged", record.ticker,
            )
            return "inconclusive"

        # ---- Record observed metrics + history days ----
        self._update_record_metrics(record, result)
        record.history_days = history_days or record.history_days

        if result.all_pass:
            # All filters passed
            failing_filter = None
            record.consecutive_fail_days = 0
            record.consecutive_pass_days = (record.consecutive_pass_days or 0) + 1

            if state == TickerState.SUSPENDED:
                if record.consecutive_pass_days >= _PASS_DAYS_TO_REACTIVATE:
                    history = record.history_days or 0
                    new_state = (
                        TickerState.ACTIVE
                        if history >= _MIN_DAYS_FOR_ACTIVE
                        else TickerState.OBSERVATION
                    )
                    record.state = new_state.value
                    record.state_since = now
                    record.suspension_reason = None
                    record.suspension_start = None
                    record.consecutive_pass_days = 0
                    logger.info(
                        "%s → %s (recovered after %d pass days)",
                        record.ticker, new_state.value, _PASS_DAYS_TO_REACTIVATE,
                    )
                    return "reactivated"
                return "suspended_recovering"

            elif state == TickerState.OBSERVATION:
                history = record.history_days or 0
                if history >= _MIN_DAYS_FOR_ACTIVE:
                    record.state = TickerState.ACTIVE.value
                    record.state_since = now
                    logger.info("%s → ACTIVE (history threshold reached)", record.ticker)
                    return "transitioned_to_active"
                return "passed"

            return "passed"

        else:
            # At least one filter failed
            failing_filter = result.primary_failing_filter
            prev_reason = record.suspension_reason

            # Hysteresis: only count if SAME filter keeps failing
            if prev_reason == failing_filter:
                record.consecutive_fail_days = (record.consecutive_fail_days or 0) + 1
            else:
                # New filter started failing — reset counter
                record.consecutive_fail_days = 1
                record.suspension_reason = failing_filter

            record.consecutive_pass_days = 0

            if record.consecutive_fail_days >= _FAIL_DAYS_TO_SUSPEND:
                if state != TickerState.SUSPENDED:
                    record.state = TickerState.SUSPENDED.value
                    record.state_since = now
                    record.suspension_start = today
                    logger.info(
                        "%s → SUSPENDED (%s for %d consecutive days)",
                        record.ticker, failing_filter, record.consecutive_fail_days,
                    )
                    return "suspended"

            logger.debug(
                "%s filter fail day %d/%d: %s",
                record.ticker, record.consecutive_fail_days,
                _FAIL_DAYS_TO_SUSPEND, result.failure_summary,
            )
            return "failed_filter"

    # ------------------------------------------------------------------
    # Internal — filter checks
    # ------------------------------------------------------------------

    def _run_filter_checks(self, ticker: str, use_intraday: bool = False) -> FilterCheckResult:
        """
        Run all 6 universe filter checks for a single ticker.

        Uses Schwab for spread/options if available; falls back to yfinance
        for market cap and volume. Checks with unavailable data return None
        (inconclusive) rather than False (failure).

        Args:
            ticker: Ticker symbol.
            use_intraday: If True, attempt to fetch intraday data for midday
                volume (requires Schwab). Ignored in monthly refresh.

        Returns:
            FilterCheckResult with all available check results.
        """
        result = FilterCheckResult(ticker=ticker, checked_at=utcnow())
        u = self._u

        # ---- Market cap (yfinance, cached 7 days) ----
        ticker_info = self._yf.get_ticker_info(ticker)
        if ticker_info and ticker_info.market_cap_mm is not None:
            mktcap = ticker_info.market_cap_mm
            result.market_cap_mm = mktcap
            result.market_cap_ok = u.market_cap_min_mm <= mktcap <= u.market_cap_max_mm

        # ---- Trading history (days) ----
        history = self._get_history_days(ticker)
        if history is not None:
            result.history_days = history
            result.history_days_ok = history >= u.min_trading_days

        # ---- Daily dollar volume (20d average, yfinance) ----
        prices = self._yf.get_daily_prices([ticker], period="1mo")
        if prices is not None and not prices.empty:
            try:
                if isinstance(prices.columns, pd.MultiIndex):
                    close = prices["Close"][ticker]
                    volume = prices["Volume"][ticker]
                else:
                    close = prices["Close"]
                    volume = prices["Volume"]
                daily_dollar_vols = (close * volume) / 1_000_000  # millions
                avg_daily_vol = float(daily_dollar_vols.tail(20).mean())
                result.daily_dollar_vol_mm = avg_daily_vol
                result.daily_dollar_vol_ok = avg_daily_vol >= u.min_daily_dollar_vol_mm
            except (KeyError, TypeError) as exc:
                logger.debug("daily_vol check failed for %s: %s", ticker, exc)

        # ---- Spread and options (Schwab, if available) ----
        if self._schwab is not None:
            # Spread from live quote
            quotes = self._schwab.get_quotes_batch([ticker])
            quote = quotes.get(ticker)
            if quote is not None:
                result.spread_bps = quote.bid_ask_spread_bps
                result.spread_ok = quote.bid_ask_spread_bps <= u.max_spread_bps

            # Options OI and strike count
            options = self._schwab.get_option_chain(ticker)
            if options is not None:
                result.options_oi = options.total_oi
                result.options_strike_count = options.strike_count_with_oi
                result.options_oi_ok = options.total_oi >= u.min_options_oi
                result.options_strike_ok = options.strike_count_with_oi >= u.min_options_strike_count

            # Midday dollar volume (simplified: use quote volume as proxy for Phase 0)
            # TODO Phase 1: fetch 5-min intraday bars from Schwab and compute
            # actual midday (10:30 AM – 2:30 PM ET) volume
            if quote is not None and result.daily_dollar_vol_mm is not None:
                # Rough proxy: assume midday represents ~30% of daily volume
                midday_approx = result.daily_dollar_vol_mm * 0.30
                result.midday_dollar_vol_mm = midday_approx
                result.midday_dollar_vol_ok = midday_approx >= u.min_midday_dollar_vol_mm
        else:
            # No Schwab — spread, options, and midday checks are inconclusive
            logger.debug(
                "%s: Schwab unavailable — spread/options/midday checks skipped", ticker
            )

        return result

    def _get_history_days(self, ticker: str) -> Optional[int]:
        """
        Estimate the number of trading days of history available for a ticker.

        Uses yfinance 5-year history length as a proxy. Returns None on failure.
        """
        prices = self._yf.get_daily_prices([ticker], period="5y")
        if prices is None or prices.empty:
            return None
        try:
            if isinstance(prices.columns, pd.MultiIndex):
                col = prices["Close"][ticker].dropna()
            else:
                col = prices["Close"].dropna()
            return len(col)
        except (KeyError, TypeError):
            return None

    def _update_record_metrics(
        self, record: UniverseState, result: FilterCheckResult
    ) -> None:
        """Persist observed filter metric values to the universe_state record."""
        if result.history_days is not None:
            record.history_days = result.history_days

    def _estimate_history_days(
        self, ticker: str, result: FilterCheckResult
    ) -> Optional[int]:
        """Return history_days from result if available, else fetch it."""
        if result.history_days is not None:
            return result.history_days
        return self._get_history_days(ticker)
