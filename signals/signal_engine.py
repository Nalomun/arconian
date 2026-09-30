"""
Signal Engine — composite scoring orchestrator.

Runs all signal components for the active + observation universe, applies
NULL propagation and weight redistribution, checks earnings/corporate-action
exclusion zones and the overrides table, then writes ALL candidate events
to signal_log regardless of whether they are ultimately traded.

Key design principles:
  - Survivorship-bias prevention: ALL candidates logged, not just strong ones.
  - Feature versioning: delay_score_as_of_signal locked at scan time.
  - OBSERVATION mode: scores computed and logged, but NOT traded.
  - NULL propagation: missing signals redistribute weight proportionally
    (see supplements Doc 3, Section 3.3).
  - Tiered scan: Pass 1 (full universe, no options) → top 40 by composite →
    Pass 2 (options enrichment for top 40 only).

Methods:
  score_universe(tickers, scan_type)  → sorted list[SignalResult]
  run_scan(scan_type)                 → get universe from DB, score, write logs

Whitepaper reference: Section 3 (Signal Detection & Scoring)
Supplements reference: Doc 3, Section 3.3 (NULL propagation)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from timeutils import utcnow
from typing import Optional

import numpy as np
import pandas as pd

from config.config_loader import ConfigLoader
from db import session_scope
from models import Override, SignalLog, UniverseState
from signals.directional_confirmation import (
    AMBIGUOUS,
    BULLISH,
    BEARISH,
    compute_directional_confirmation,
)
from signals.options_signal import OptionsSignalResult, compute_options_signal
from signals.retail_attention import compute_retail_attention
from signals.return_signal import compute_return_signal
from signals.sector_rs_signal import build_spread_history, compute_5d_return, compute_sector_rs_signal
from signals.volume_signal import VolumeSignalResult, compute_volume_signal
from universe.universe_state import TickerState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SignalResult dataclass — one per ticker per scan
# ---------------------------------------------------------------------------

@dataclass
class SignalResult:
    """
    Full scoring output for a single ticker at a single scan time.

    This maps 1:1 to a row in signal_log. Outcome fields (return_1d etc.)
    are populated later by journal/outcome_collector.py.
    """

    ticker: str
    scan_timestamp: datetime
    scan_type: str               # 'open' | 'midday' | 'preclose'

    # ---- Feature vector ----
    volume_pctile_20d: Optional[float] = None
    volume_pctile_60d: Optional[float] = None
    volume_pctile_120d: Optional[float] = None
    return_pctile_60d: Optional[float] = None
    options_composite: Optional[float] = None
    sector_rs_percentile: Optional[float] = None
    delay_score: Optional[float] = None

    # ---- Composite ----
    retail_attention_score: Optional[float] = None
    composite_score_raw: Optional[float] = None   # before retail penalty
    composite_score: Optional[float] = None        # after retail penalty

    # ---- Effective weights at scan time ----
    weight_volume: Optional[float] = None
    weight_return: Optional[float] = None
    weight_options: Optional[float] = None
    weight_sector_rs: Optional[float] = None
    weight_delay: Optional[float] = None

    # ---- Context ----
    history_status: str = "full"              # 'full' | 'short'
    direction_signal: Optional[str] = None    # 'bullish' | 'bearish' | 'ambiguous'
    prior_day_vwap: Optional[float] = None
    borrow_available: Optional[bool] = None

    # ---- Exclusions ----
    earnings_proximity_tag: Optional[str] = None   # 'excluded' | 'post_earnings_drift' | 'normal'
    corporate_action_flag: bool = False
    corporate_action_type: Optional[str] = None
    catalyst_flag: bool = False
    was_excluded: bool = False                     # True if not scored (earnings/corp action)
    exclusion_reason: Optional[str] = None

    # ---- Market context ----
    market_cap_mm: Optional[float] = None
    sector_etf: Optional[str] = None
    avg_daily_dollar_vol: Optional[float] = None
    midday_dollar_vol: Optional[float] = None
    bid_ask_spread_bps: Optional[float] = None
    atr_20: Optional[float] = None
    days_to_next_earnings: Optional[int] = None
    days_since_last_earnings: Optional[int] = None

    # ---- Universe state ----
    is_observation_mode: bool = False   # True if OBSERVATION state (not traded)
    return_1d: Optional[float] = None          # today's raw 1d return (for directional confirmation)
    config_hash: Optional[str] = None


# ---------------------------------------------------------------------------
# Composite score computation with NULL propagation
# ---------------------------------------------------------------------------

def compute_composite(
    signals: dict[str, Optional[float]],
    weights: dict[str, float],
    retail_penalty: float,
    penalty_weight: float,
) -> tuple[Optional[float], Optional[float], dict[str, float]]:
    """
    Compute the composite score with NULL propagation and retail penalty.

    Signals whose value is None are excluded; remaining weights are
    renormalised to sum to 1.0 before weighting. This is the exact formula
    from Doc 3, Section 3.3.

    Args:
        signals: Dict of signal_name → value (None = unavailable).
                 Keys: 'volume', 'return', 'options', 'sector_rs', 'delay'
        weights: Configured weights (may not sum to 1.0 — the config stores
                 nominal weights; normalisation happens here at runtime).
        retail_penalty: Retail attention score [0, 1]. Use 0.0 if unavailable.
        penalty_weight: Maximum fractional reduction from retail penalty.

    Returns:
        (raw_composite, penalized_composite, effective_weight_dict)
        Both composites are None if no signals are available.
    """
    available = {k: v for k, v in signals.items() if v is not None}
    if not available:
        return None, None, {}

    active_w = {k: weights[k] for k in available if k in weights}
    total_w = sum(active_w.values())
    if total_w == 0:
        return None, None, {}

    norm_w = {k: w / total_w for k, w in active_w.items()}
    raw_composite = sum(norm_w[k] * available[k] for k in available if k in norm_w)
    raw_composite = float(np.clip(raw_composite, 0.0, 1.0))

    effective_penalty = float(retail_penalty) if retail_penalty is not None else 0.0
    penalized = raw_composite * (1.0 - penalty_weight * effective_penalty)
    penalized = float(np.clip(penalized, 0.0, 1.0))

    return raw_composite, penalized, norm_w


# ---------------------------------------------------------------------------
# ATR helper
# ---------------------------------------------------------------------------

def _compute_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 20) -> Optional[float]:
    """
    Average True Range over the last `period` days.

    True range = max(high−low, |high−prev_close|, |low−prev_close|)
    """
    h = np.asarray(high, dtype=float)
    lo = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)

    if len(c) < period + 1:
        return None

    tr_list = []
    for i in range(1, len(c)):
        tr = max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
        tr_list.append(tr)

    atr_arr = np.array(tr_list)
    return float(np.mean(atr_arr[-period:]))


# ---------------------------------------------------------------------------
# SignalEngine class
# ---------------------------------------------------------------------------

class SignalEngine:
    """
    Orchestrates the full signal scoring pipeline.

    Phase 0 / 1: computes all signals and writes to signal_log. No orders placed.
    Phase 2+: the output (sorted list[SignalResult]) feeds the risk engine.

    Args:
        config: Loaded ConfigLoader instance.
        yf_adapter: YFinanceAdapter for price and fundamental data.
        schwab_adapter: SchwabAdapter for live quotes and options chains.
            If None, spread/options signals default to neutral.
        social_adapter: SocialAdapter for retail attention data. If None,
            retail attention defaults to 0.0 (no penalty).
        edgar_adapter: EdgarAdapter for corporate action detection. If None,
            corporate action filtering is skipped (not blocked).
        earnings_calendar: EarningsCalendar instance. If None, earnings
            exclusion zones are skipped.
    """

    def __init__(
        self,
        config: ConfigLoader,
        yf_adapter,
        schwab_adapter=None,
        social_adapter=None,
        edgar_adapter=None,
        earnings_calendar=None,
    ) -> None:
        self._config = config
        self._yf = yf_adapter
        self._schwab = schwab_adapter
        self._social = social_adapter
        self._edgar = edgar_adapter
        self._earnings = earnings_calendar
        self._s = config.signal
        self._u = config.universe
        logger.info("SignalEngine initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score_universe(
        self,
        tickers: list[str],
        scan_type: str = "midday",
        pass1_top_n: Optional[int] = None,
    ) -> list[SignalResult]:
        """
        Score a list of tickers and return results sorted by composite_score
        descending (ACTIVE tickers first, then OBSERVATION).

        This is a two-pass tiered scan:
          Pass 1: volume + return + sector RS + delay for all tickers
          Pass 2: options enrichment for top pass1_top_n by Pass 1 score

        Args:
            tickers: Ticker symbols to score. Typically the ACTIVE +
                OBSERVATION universe from UniverseManager.
            scan_type: 'open', 'midday', or 'preclose'.
            pass1_top_n: How many top Pass 1 tickers get options enrichment.
                Defaults to config.execution.pass1_top_n.

        Returns:
            List of SignalResult sorted by composite_score descending.
            Excluded events (earnings, corporate action) are included in
            the list with was_excluded=True and composite_score=None.
        """
        if not tickers:
            return []

        n_pass1 = pass1_top_n or self._config.execution.pass1_top_n
        scan_ts = utcnow()
        logger.info("SignalEngine.score_universe: %d tickers, scan_type=%s", len(tickers), scan_type)

        # ---- Fetch overrides (blocked + corp-action tickers) ----
        blocked, corp_actions = self._load_overrides()

        # ---- Fetch universe state (delay scores, history_days) ----
        universe_states = self._load_universe_states(tickers)

        # ---- Bulk price fetch (Pass 1 data) ----
        # 1y (~252 sessions), NOT 6mo: the 120-day volume window needs 121 bars,
        # and 6mo (~124-126 sessions) leaves only ~4 bars of margin — a few
        # missing rows tipped a full-history ticker into history_status='short',
        # which silently makes it ineligible for auto-trading with no log trail
        # (F-20). 1y gives ample headroom so only genuinely short tickers flag.
        price_df = self._yf.get_daily_prices(tickers, period="1y")
        sector_etf_df = self._yf.get_sector_etf_prices(period="3mo")

        # ---- Pass 1: compute volume, return, sector RS for all ----
        results: list[SignalResult] = []
        for ticker in tickers:
            # Per-ticker isolation (F-9): a single bad ticker (e.g. misaligned or
            # corrupt price rows) must not abort the entire scan. On failure we
            # still emit a row so the event is logged (survivorship principle),
            # marked excluded with reason 'scoring_error'.
            try:
                result = self._score_ticker_pass1(
                    ticker=ticker,
                    scan_ts=scan_ts,
                    scan_type=scan_type,
                    price_df=price_df,
                    sector_etf_df=sector_etf_df,
                    universe_states=universe_states,
                    blocked=blocked,
                    corp_actions=corp_actions,
                )
            except Exception:
                logger.exception("score_ticker_pass1 failed for %s — skipping", ticker)
                result = SignalResult(ticker=ticker, scan_timestamp=scan_ts, scan_type=scan_type)
                result.was_excluded = True
                result.exclusion_reason = "scoring_error"
            results.append(result)

        # ---- Pass 2: options enrichment for top n (non-excluded, non-blocked) ----
        # Decision #6 / F-4: the options sub-signal is SUPPRESSED until the
        # per-ticker IV-rank / put-call history store exists (a Phase-2 build).
        # Without that history every sub-component defaults to 0.5, so the
        # options composite was a constant 0.5 — not neutral but distorting,
        # because weight-renormalisation then pulled genuine high-scorers toward
        # 0.5. With options_enabled=False, options_composite stays None and the
        # existing NULL propagation redistributes its 15% across the four working
        # signals (the documented behaviour). Flip options_enabled=True only once
        # the IV/PC history store is in place.
        if self._schwab is not None and getattr(self._s, "options_enabled", False):
            eligible = [r for r in results if not r.was_excluded and r.composite_score is not None]
            eligible_sorted = sorted(eligible, key=lambda r: r.composite_score or 0.0, reverse=True)
            top_n = eligible_sorted[:n_pass1]
            top_tickers = [r.ticker for r in top_n]

            if top_tickers:
                self._enrich_with_options(
                    results_by_ticker={r.ticker: r for r in top_n},
                    scan_ts=scan_ts,
                )

        # ---- Directional confirmation ----
        for result in results:
            if not result.was_excluded and result.composite_score is not None:
                self._add_directional_confirmation(result)

        # ---- Retail attention penalty ----
        self._apply_retail_attention(results)

        # ---- Sort: scored ACTIVE first, then OBSERVATION, excluded last ----
        results.sort(
            key=lambda r: (
                r.was_excluded,
                r.is_observation_mode,
                -(r.composite_score or 0.0),
            )
        )

        return results

    def run_scan(self, scan_type: str = "midday") -> list[SignalResult]:
        """
        Full production scan: loads active + observation universe from DB,
        scores everything, writes all events to signal_log, and returns the
        ranked list.

        Args:
            scan_type: 'open', 'midday', or 'preclose'.

        Returns:
            Sorted list[SignalResult] — same as score_universe output.
        """
        # Load universe from DB
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
            tickers = [r.ticker for r in records]

        if not tickers:
            logger.info("run_scan: no tickers in ACTIVE/OBSERVATION state")
            return []

        logger.info("run_scan: scoring %d tickers (%s)", len(tickers), scan_type)
        results = self.score_universe(tickers, scan_type=scan_type)

        # Write all events to signal_log
        self._write_signal_log(results)

        # Log ranked candidates to stdout/log
        scored = [r for r in results if r.composite_score is not None and not r.was_excluded]
        logger.info(
            "run_scan complete: %d scored, %d excluded",
            len(scored), sum(1 for r in results if r.was_excluded),
        )
        for i, r in enumerate(scored[:20]):   # top 20 to log
            logger.info(
                "  #%02d %-6s score=%.3f dir=%-9s obs=%s",
                i + 1, r.ticker,
                r.composite_score,
                r.direction_signal or "n/a",
                "Y" if r.is_observation_mode else "N",
            )

        return results

    # ------------------------------------------------------------------
    # Pass 1 scoring
    # ------------------------------------------------------------------

    def _score_ticker_pass1(
        self,
        ticker: str,
        scan_ts: datetime,
        scan_type: str,
        price_df,
        sector_etf_df,
        universe_states: dict[str, UniverseState],
        blocked: set[str],
        corp_actions: dict[str, str],
    ) -> SignalResult:
        """Compute volume, return, and sector RS for a single ticker."""
        result = SignalResult(ticker=ticker, scan_timestamp=scan_ts, scan_type=scan_type)
        result.config_hash = getattr(self._config, "config_hash", None)

        state_record = universe_states.get(ticker)
        if state_record:
            result.is_observation_mode = state_record.state == TickerState.OBSERVATION.value
            result.delay_score = state_record.delay_score
            history_days = state_record.history_days or 0
        else:
            history_days = 0

        # History status
        result.history_status = "short" if result.is_observation_mode else "full"

        # ---- Corporate action flag ----
        if ticker in blocked:
            result.was_excluded = True
            result.exclusion_reason = "blocked_override"
            result.corporate_action_flag = True
            result.corporate_action_type = corp_actions.get(ticker)
            return result

        if ticker in corp_actions:
            result.corporate_action_flag = True
            result.corporate_action_type = corp_actions[ticker]
            result.catalyst_flag = True

        # ---- Earnings exclusion ----
        self._check_earnings(result)
        if result.was_excluded:
            return result

        # ---- Ticker info (market cap, sector, SI) ----
        ticker_info = self._yf.get_ticker_info(ticker) if self._yf else None
        if ticker_info:
            result.market_cap_mm = ticker_info.market_cap_mm
            result.sector_etf = ticker_info.sector_etf
            # (short interest is fetched where it is used, in _apply_retail_attention)

        # ---- Extract price/volume data ----
        closes, volumes, highs, lows = self._extract_ohlcv(price_df, ticker)
        if closes is None or len(closes) < 2:
            logger.debug("score_ticker: no price data for %s", ticker)
            return result

        # ATR
        if highs is not None and lows is not None:
            result.atr_20 = _compute_atr(highs, lows, closes)

        # Daily dollar volumes (close × volume, in millions)
        if volumes is not None:
            dollar_vols = (closes * volumes) / 1_000_000
            if len(dollar_vols) >= 2:
                result.avg_daily_dollar_vol = float(np.nanmean(dollar_vols[-20:]))

        # ---- Volume signal ----
        if volumes is not None and len(closes) >= 2:
            today_close = float(closes[-1])
            today_vol = float(volumes[-1]) if len(volumes) > 0 else 0.0
            today_dv = today_close * today_vol / 1_000_000
            history_dv = (closes[:-1] * volumes[:-1]) / 1_000_000 if volumes is not None else np.array([])

            vol_result = compute_volume_signal(today_dv, history_dv)
            result.volume_pctile_20d = vol_result.pctile_20d
            result.volume_pctile_60d = vol_result.pctile_60d
            result.volume_pctile_120d = vol_result.pctile_120d
            if vol_result.short_history:
                result.history_status = "short"

        # ---- Return signal ----
        if len(closes) >= 2:
            today_ret = float(closes[-1] / closes[-2] - 1.0)
            result.return_1d = today_ret
            hist_abs_rets = np.abs(np.diff(closes[:-1]) / closes[:-2]) if len(closes) > 2 else np.array([])
            result.return_pctile_60d = compute_return_signal(abs(today_ret), hist_abs_rets)

        # ---- Sector RS signal ----
        if result.sector_etf and sector_etf_df is not None:
            etf_closes = self._extract_etf_prices(sector_etf_df, result.sector_etf)
            if etf_closes is not None and len(etf_closes) >= 6:
                stock_5d = compute_5d_return(closes)
                etf_5d = compute_5d_return(etf_closes)
                # Tail-align (F-8): the stock series is 6mo and the ETF series
                # 3mo. Slicing [:min_len] paired the OLDEST stock days against the
                # ETF's most RECENT days — a meaningless spread history. Both
                # series end on the latest available session, so align on the
                # most recent min_len days. (Date-indexed alignment would be
                # strictly better but the arrays are de-indexed by this point.)
                min_len = min(len(closes), len(etf_closes))
                history_spreads = build_spread_history(closes[-min_len:], etf_closes[-min_len:])
                result.sector_rs_percentile = compute_sector_rs_signal(stock_5d, etf_5d, history_spreads)

        # ---- Pass 1 composite (without options) ----
        self._recompute_composite(result)

        return result

    # ------------------------------------------------------------------
    # Pass 2 — options enrichment
    # ------------------------------------------------------------------

    def _enrich_with_options(
        self,
        results_by_ticker: dict[str, SignalResult],
        scan_ts: datetime,
    ) -> None:
        """Fetch options chains for top-N tickers and enrich their results."""
        for ticker, result in results_by_ticker.items():
            try:
                options = self._schwab.get_option_chain(ticker)
                if options is None:
                    continue

                opt_result = compute_options_signal(
                    total_call_vol=options.total_call_volume,
                    total_put_vol=options.total_put_volume,
                    total_oi=options.total_oi,
                    strike_count_with_oi=options.strike_count_with_oi,
                    distinct_lot_sizes=options.distinct_lot_sizes,
                    current_iv=options.weighted_avg_iv,
                    min_daily_volume=self._s.options_min_daily_volume,
                    min_oi=self._u.min_options_oi,
                    min_strikes=self._u.min_options_strike_count,
                    min_trade_sizes=self._s.options_min_trade_sizes,
                    iv_range_min_ppt=self._s.iv_range_min_pct_points,
                )

                result.options_composite = opt_result.composite
                # Recompute composite now that options may be available
                self._recompute_composite(result)

            except Exception as exc:
                logger.warning("options enrichment failed for %s: %s", ticker, exc)

    # ------------------------------------------------------------------
    # Directional confirmation
    # ------------------------------------------------------------------

    def _add_directional_confirmation(self, result: SignalResult) -> None:
        """Add directional classification to a scored SignalResult."""
        if result.return_1d is None:
            result.direction_signal = AMBIGUOUS
            return

        # TODO Phase 1: compute prior_day_vwap from Schwab 5-min bars (spec Section 3.4).
        # Phase 0: prior_day_vwap is None → VWAP condition is treated as inconclusive
        # (not a blocking condition) by compute_directional_confirmation.
        result.direction_signal = compute_directional_confirmation(
            return_1d=result.return_1d,
            current_price=0.0,             # price not needed when prior_day_vwap is None
            prior_day_vwap=result.prior_day_vwap,
            call_vol=None,                 # options directional set during Pass 2 enrichment
            put_vol=None,
            borrow_available=result.borrow_available,
        )

    # ------------------------------------------------------------------
    # Retail attention
    # ------------------------------------------------------------------

    def _apply_retail_attention(self, results: list[SignalResult]) -> None:
        """
        Compute retail attention penalty for every non-excluded result.

        The score is always numeric (per §3.3 NULL propagation) — missing
        sub-components default to 0 inside compute_retail_attention. When
        the social adapter is unavailable the SI sub-component still
        contributes; nothing about that fallback should produce NULL.
        """
        for result in results:
            if result.was_excluded:
                continue
            try:
                # Pull SI from the yfinance ticker info cache (ticker_info is
                # cached upstream, so this is cheap on the second call).
                ticker_info = self._yf.get_ticker_info(result.ticker) if self._yf else None
                si_pct = ticker_info.short_pct_float if ticker_info else None

                mentions: Optional[float] = None
                scanner_flag: Optional[bool] = None

                if self._social is not None:
                    try:
                        social_vel = self._social.get_social_velocity(result.ticker)
                        scanner_flag = bool(
                            getattr(social_vel, "is_trending_stocktwits", False)
                        )
                        st = getattr(social_vel, "stocktwits_mentions_24h", 0.0) or 0.0
                        rd = getattr(social_vel, "reddit_mentions_24h", 0.0) or 0.0
                        mentions = float(st) + float(rd)
                    except Exception as exc:
                        logger.warning(
                            "social_adapter call failed for %s: %s — falling back to SI-only",
                            result.ticker, exc,
                        )

                ra = compute_retail_attention(
                    mentions_24h=mentions,
                    history_mentions=None,
                    scanner_flag=scanner_flag,
                    short_interest_pct=si_pct,
                )
                result.retail_attention_score = ra.score

                # Recompute composite with penalty
                if result.composite_score_raw is not None:
                    pw = self._s.retail_penalty_weight
                    penalized = result.composite_score_raw * (1.0 - pw * ra.score)
                    result.composite_score = float(np.clip(penalized, 0.0, 1.0))

            except Exception as exc:
                logger.warning("retail attention failed for %s: %s", result.ticker, exc)
                # Even on unexpected failure, default to 0 rather than NULL
                # so the column is always populated.
                if result.retail_attention_score is None:
                    result.retail_attention_score = 0.0

    # ------------------------------------------------------------------
    # Signal_log persistence
    # ------------------------------------------------------------------

    def _write_signal_log(self, results: list[SignalResult]) -> None:
        """Write all SignalResult objects to signal_log in a single transaction."""
        entries = []
        for r in results:
            entry = SignalLog(
                scan_timestamp=r.scan_timestamp,
                scan_type=r.scan_type,
                ticker=r.ticker,
                volume_pctile_20d=r.volume_pctile_20d,
                volume_pctile_60d=r.volume_pctile_60d,
                volume_pctile_120d=r.volume_pctile_120d,
                return_pctile_60d=r.return_pctile_60d,
                options_composite=r.options_composite,
                sector_rs_percentile=r.sector_rs_percentile,
                delay_score=r.delay_score,
                delay_score_as_of_signal=r.delay_score,
                retail_attention_score=r.retail_attention_score,
                composite_score_raw=r.composite_score_raw,
                composite_score=r.composite_score,
                weight_volume=r.weight_volume,
                weight_return=r.weight_return,
                weight_options=r.weight_options,
                weight_sector_rs=r.weight_sector_rs,
                weight_delay=r.weight_delay,
                market_cap_mm=r.market_cap_mm,
                sector_etf=r.sector_etf,
                avg_daily_dollar_vol=r.avg_daily_dollar_vol,
                midday_dollar_vol=r.midday_dollar_vol,
                bid_ask_spread_bps=r.bid_ask_spread_bps,
                atr_20=r.atr_20,
                days_to_next_earnings=r.days_to_next_earnings,
                days_since_last_earnings=r.days_since_last_earnings,
                earnings_proximity_tag=r.earnings_proximity_tag,
                corporate_action_flag=r.corporate_action_flag,
                corporate_action_type=r.corporate_action_type,
                catalyst_flag=r.catalyst_flag,
                history_status=r.history_status,
                direction_signal=r.direction_signal,
                prior_day_vwap=r.prior_day_vwap,
                borrow_available=r.borrow_available,
                was_traded=False,
            )
            entries.append(entry)

        if not entries:
            return

        try:
            with session_scope() as session:
                for entry in entries:
                    session.add(entry)
            logger.info("signal_log: wrote %d entries", len(entries))
        except Exception as exc:
            logger.error("signal_log write failed: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_overrides(self) -> tuple[set[str], dict[str, str]]:
        """
        Returns:
            (blocked_tickers, corp_action_tickers)
            blocked: tickers with active 'block' override → excluded entirely
            corp_actions: ticker → action_type for active 'corp_action_manual' overrides
        """
        blocked: set[str] = set()
        corp_actions: dict[str, str] = {}
        try:
            with session_scope() as session:
                overrides = (
                    session.query(Override)
                    .filter(Override.active.is_(True))
                    .all()
                )
                for o in overrides:
                    if o.override_type == "block":
                        blocked.add(o.ticker)
                    elif o.override_type == "corp_action_manual":
                        corp_actions[o.ticker] = o.reason or "manual"
        except Exception as exc:
            logger.error("_load_overrides failed: %s", exc)
        return blocked, corp_actions

    def _load_universe_states(self, tickers: list[str]) -> dict[str, UniverseState]:
        """Load UniverseState records for the given tickers."""
        try:
            with session_scope() as session:
                records = (
                    session.query(UniverseState)
                    .filter(UniverseState.ticker.in_(tickers))
                    .all()
                )
                return {r.ticker: r for r in records}
        except Exception as exc:
            logger.error("_load_universe_states failed: %s", exc)
            return {}

    def _check_earnings(self, result: SignalResult) -> None:
        """
        Check earnings exclusion zones and tag result accordingly.

        Mutates result.earnings_proximity_tag and result.was_excluded.
        """
        if self._earnings is None:
            result.earnings_proximity_tag = "normal"
            return

        try:
            # Calendar-day distances are stored on the row for reference.
            result.days_to_next_earnings = self._earnings.days_to_next_earnings(result.ticker)
            result.days_since_last_earnings = self._earnings.days_since_last_earnings(result.ticker)

            # The exclusion windows are TRADING days (config comment + hard
            # constraint #3), so the decision must compare against trading-day
            # distances. Using calendar days (F-15) missed the most common case:
            # a Friday reporter is 3 calendar days but only 1 trading day from
            # the following Monday, so it was wrongly scored on Monday.
            td_next = self._earnings.trading_days_to_next_earnings(result.ticker)
            td_since = self._earnings.trading_days_since_last_earnings(result.ticker)

            excl_before = self._s.earnings_exclusion_days_before
            excl_after = self._s.earnings_exclusion_days_after

            if td_next is not None and 0 <= td_next <= excl_before:
                result.earnings_proximity_tag = "excluded"
                result.was_excluded = True
                result.exclusion_reason = f"earnings in {td_next} trading day(s)"
            elif td_since is not None and 0 <= td_since <= excl_after:
                result.earnings_proximity_tag = "excluded"
                result.was_excluded = True
                result.exclusion_reason = f"earnings {td_since} trading day(s) ago"
            elif td_since is not None and td_since <= 5:
                result.earnings_proximity_tag = "post_earnings_drift"
                result.catalyst_flag = True
            else:
                result.earnings_proximity_tag = "normal"
        except Exception as exc:
            logger.debug("_check_earnings failed for %s: %s", result.ticker, exc)
            result.earnings_proximity_tag = "normal"

    def _recompute_composite(self, result: SignalResult) -> None:
        """
        (Re)compute composite_score_raw and composite_score with current
        signal values and apply the configured weights + NULL propagation.
        """
        s = self._s
        base_weights = {
            "volume":    s.weight_volume,
            "return":    s.weight_return,
            "options":   s.weight_options,
            "sector_rs": s.weight_sector_rs,
            "delay":     s.weight_delay,
        }
        vol_values = [x for x in [result.volume_pctile_20d, result.volume_pctile_60d,
                                   result.volume_pctile_120d] if x is not None]
        volume_composite = float(np.mean(vol_values)) if vol_values else None

        signals = {
            "volume":    volume_composite,
            "return":    result.return_pctile_60d,
            "options":   result.options_composite,
            "sector_rs": result.sector_rs_percentile,
            "delay":     result.delay_score,
        }

        penalty = result.retail_attention_score or 0.0
        pw = s.retail_penalty_weight

        raw, penalized, norm_w = compute_composite(signals, base_weights, penalty, pw)
        result.composite_score_raw = raw
        result.composite_score = penalized

        # Store effective weights (after redistribution)
        result.weight_volume    = norm_w.get("volume")
        result.weight_return    = norm_w.get("return")
        result.weight_options   = norm_w.get("options")
        result.weight_sector_rs = norm_w.get("sector_rs")
        result.weight_delay     = norm_w.get("delay")

    def _extract_ohlcv(
        self,
        price_df,
        ticker: str,
    ) -> tuple[Optional[np.ndarray], Optional[np.ndarray], Optional[np.ndarray], Optional[np.ndarray]]:
        """
        Extract aligned close, volume, high, low arrays for a single ticker
        from a potentially multi-index yfinance DataFrame.

        Returns (closes, volumes, highs, lows) as numpy arrays, or (None, None, None, None).
        """
        if price_df is None or price_df.empty:
            return None, None, None, None

        try:
            if isinstance(price_df.columns, pd.MultiIndex):
                close = price_df["Close"].get(ticker)
                volume = price_df["Volume"].get(ticker)
                high = price_df["High"].get(ticker)
                low = price_df["Low"].get(ticker)
            else:
                if ticker not in price_df.columns:
                    return None, None, None, None
                close = price_df[ticker]
                volume = high = low = None

            if close is None:
                return None, None, None, None

            # Assemble available columns into one frame and drop any row where
            # ANY column is NaN, so every returned array is equal-length and
            # date-aligned. Independent per-column dropna() (the old code) could
            # return arrays of different lengths AND shifted dates, which then
            # corrupted `closes * volumes` and ATR indexing, or raised and killed
            # the whole scan (F-9).
            cols = {"close": close}
            if volume is not None:
                cols["volume"] = volume
            if high is not None:
                cols["high"] = high
            if low is not None:
                cols["low"] = low
            frame = pd.DataFrame(cols).dropna()
            if len(frame) < 2:
                return None, None, None, None

            closes = frame["close"].values.astype(float)
            volumes = frame["volume"].values.astype(float) if "volume" in frame else None
            highs = frame["high"].values.astype(float) if "high" in frame else None
            lows = frame["low"].values.astype(float) if "low" in frame else None

            return closes, volumes, highs, lows

        except Exception as exc:
            logger.debug("_extract_ohlcv failed for %s: %s", ticker, exc)
            return None, None, None, None

    def _extract_etf_prices(self, sector_etf_df, etf_ticker: str) -> Optional[np.ndarray]:
        """Extract close price array for a sector ETF from a multi-index DataFrame."""
        if sector_etf_df is None or sector_etf_df.empty:
            return None
        try:
            if isinstance(sector_etf_df.columns, pd.MultiIndex):
                col = sector_etf_df["Close"].get(etf_ticker)
            else:
                col = sector_etf_df.get(etf_ticker)

            if col is None:
                return None
            arr = col.dropna().values.astype(float)
            return arr if len(arr) >= 6 else None
        except Exception as exc:
            logger.debug("_extract_etf_prices failed for %s: %s", etf_ticker, exc)
            return None
