"""
Outcome Collector — daily 4:15 PM after-close task.

For each scored signal_log event that is ≥ time_stop_days old and has no
return_3d yet:

  1. Fetches daily OHLC via yfinance for day 0 through day 3 after the signal.
  2. Writes one OutcomePrice row per trading day (day_offset 0..3).
  3. Computes and writes back to signal_log:
       return_1d, return_3d, max_adverse_excursion, max_favorable_excursion
  4. Applies triple-barrier labeling:
       +1  upper barrier hit  (return_3d > stop_fraction  from target side)
       -1  lower barrier hit  (any day's low breaches stop proxy)
        0  time barrier       (neither barrier hit within 3 days)

  For Phase 0 (untraded signals), entry_price is NULL so we proxy it as
  the close on the signal day (day 0). The barriers are approximated using
  the position's atr_20 field stored in signal_log.

Whitepaper reference: Section 7.2 (outcome_prices), Section 6.2 (triple-barrier labeling)
"""

import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Fraction of ATR used as the barrier width when no explicit stop/target is stored.
# For a 1.5×ATR stop, this is 1/(1.5 × position_value) but we simplify to a
# percentage-of-price estimate: default barriers at ±3% (approx 2×ATR on a
# typical small-cap at $0.50 ATR / $20 price = 2.5%).
_DEFAULT_BARRIER_PCT = 0.03   # ±3% proxy barrier


def run(
    session_scope: Callable,
    yf_adapter,
    time_stop_days: int = 3,
    max_age_days: int = 30,
) -> dict:
    """
    Daily outcome collection job. Called after market close.

    Args:
        session_scope: Session context manager.
        yf_adapter: YFinanceAdapter instance for price fetching.
        time_stop_days: Days in the hold window (from config.execution.time_stop_days).
        max_age_days: Rows still unresolved past this age are abandoned (F-24)
            so they can't clog the oldest-first queue.

    Returns:
        Summary dict: {processed, skipped, errors, abandoned}.
    """
    from journal.signal_log import get_unresolved_events, count_abandoned_events

    events = get_unresolved_events(
        session_scope=session_scope,
        min_age_days=time_stop_days,
        max_age_days=max_age_days,
    )

    processed = skipped = errors = 0

    for event in events:
        try:
            ok = _process_event(event, session_scope, yf_adapter)
            if ok:
                processed += 1
            else:
                skipped += 1
        except Exception:
            logger.error(
                "outcome_collector: unhandled error for signal_id=%d ticker=%s",
                event.id, event.ticker, exc_info=True,
            )
            errors += 1

    # Dead-letter audit (F-24): surface rows we've given up on so a silent
    # delisting wave doesn't just vanish from the pipeline.
    abandoned = count_abandoned_events(session_scope, max_age_days=max_age_days)
    if abandoned:
        logger.warning(
            "outcome_collector: %d unresolved signal(s) older than %d days abandoned "
            "(likely delisted/halted) — no longer retried",
            abandoned, max_age_days,
        )

    logger.info(
        "outcome_collector: processed=%d skipped=%d errors=%d abandoned=%d",
        processed, skipped, errors, abandoned,
    )
    return {"processed": processed, "skipped": skipped, "errors": errors,
            "abandoned": abandoned}


def _process_event(event, session_scope: Callable, yf_adapter) -> bool:
    """
    Fetch OHLC, write OutcomePrice rows, compute and back-fill return fields.

    Returns True if the event was resolved, False if data was unavailable.
    """
    ticker = event.ticker
    signal_date = event.scan_timestamp.date()

    # Fetch enough history to cover signal_date + 3 trading days
    fetch_end = signal_date + timedelta(days=7)  # buffer for weekends/holidays
    fetch_start = signal_date - timedelta(days=1)  # include day-1 for return calc

    try:
        hist = yf_adapter.get_price_history(
            ticker=ticker,
            start=fetch_start.strftime("%Y-%m-%d"),
            end=fetch_end.strftime("%Y-%m-%d"),
            interval="1d",
        )
    except Exception:
        logger.warning("outcome_collector: price fetch failed for %s", ticker, exc_info=True)
        return False

    if hist is None or hist.empty:
        logger.debug("outcome_collector: no price data for %s", ticker)
        return False

    # Normalise to a list of (date, high, low, close) sorted chronologically
    try:
        daily = _extract_ohlc(hist)
    except Exception:
        logger.warning("outcome_collector: OHLC extraction failed for %s", ticker)
        return False

    # Find the index of the signal date (day 0)
    dates = [d for d, h, l, c in daily]
    try:
        day0_idx = next(i for i, d in enumerate(dates) if d >= signal_date)
    except StopIteration:
        logger.debug("outcome_collector: signal date %s not found in history for %s", signal_date, ticker)
        return False

    # F-24: if the signal day itself has no bar (data gap / halt), the next
    # available session silently becomes "day 0". Surface that rather than
    # labeling against a shifted window without anyone knowing.
    if dates[day0_idx] != signal_date:
        logger.warning(
            "outcome_collector: %s has no bar on signal date %s — first bar is %s; "
            "labeling against the shifted window",
            ticker, signal_date, dates[day0_idx],
        )

    # Collect day 0 through day 3 (4 bars). The full window is required before
    # labeling: the old `< 2` guard wrote premature time-barrier 0s and day-early
    # excursions for Thu/Fri signals (F-11). Fewer than 4 bars → defer; the event
    # stays unresolved and is retried on a later run once the data completes.
    day_rows = daily[day0_idx: day0_idx + 4]
    if len(day_rows) < 4:
        logger.debug(
            "outcome_collector: deferring %s — only %d/4 post-signal bars available",
            ticker, len(day_rows),
        )
        return False

    # Write OutcomePrice rows
    _write_outcome_prices(event.id, day_rows, session_scope)

    # Compute return metrics
    entry_proxy = day_rows[0][3]  # close on signal day as entry proxy
    if entry_proxy <= 0:
        return False

    closes = [c for _, h, l, c in day_rows]
    highs  = [h for _, h, l, c in day_rows]
    lows   = [l for _, h, l, c in day_rows]

    direction = event.direction_signal or "bullish"
    is_long = direction != "bearish"

    return_1d = (closes[1] - entry_proxy) / entry_proxy if len(closes) >= 2 else None
    return_3d = (closes[-1] - entry_proxy) / entry_proxy if len(closes) >= 4 else None

    # MAE / MFE over the post-entry window (days 1..3). Day 0's intraday range is
    # excluded because the entry proxy is the day-0 *close*; including day 0 would
    # inflate MFE on spike days. This matches the barrier loop, which also skips
    # day 0 (F-11).
    post_highs = highs[1:]
    post_lows = lows[1:]
    if is_long:
        mfe = max((h - entry_proxy) / entry_proxy for h in post_highs)
        mae = min((l - entry_proxy) / entry_proxy for l in post_lows)
    else:
        mfe = max((entry_proxy - l) / entry_proxy for l in post_lows)
        mae = min((entry_proxy - h) / entry_proxy for h in post_highs)

    # F-6: use the trade's REAL stop for traded rows. The old code passed
    # event.entry_price (the fill price) as the stop barrier, so any later bar
    # touching the entry labeled the row -1 regardless of the true outcome.
    # Untraded rows keep stop_price=None → ATR-proxy barrier.
    stop_price = None
    if getattr(event, "was_traded", False) and getattr(event, "trade_id", None) is not None:
        from models import Trade
        try:
            with session_scope() as session:
                trade = session.get(Trade, event.trade_id)
                stop_price = trade.stop_price if trade is not None else None
        except Exception:
            logger.warning(
                "outcome_collector: failed to load trade stop for signal_id=%d",
                event.id, exc_info=True,
            )

    # Triple-barrier label
    label = _triple_barrier_label(
        entry_proxy=entry_proxy,
        is_long=is_long,
        highs=highs[1:],  # exclude day 0 (entry day)
        lows=lows[1:],
        atr_20=event.atr_20,
        stop_price=stop_price,    # real trade stop, or None → ATR proxy
        target_price=None,
    )

    from journal.signal_log import update_outcomes
    update_outcomes(
        session_scope=session_scope,
        signal_id=event.id,
        return_1d=return_1d,
        return_3d=return_3d,
        max_adverse_excursion=mae,
        max_favorable_excursion=mfe,
        outcome_label=label,
    )

    logger.debug(
        "outcome_collector: %s id=%d return_3d=%.3f label=%s",
        ticker, event.id, return_3d or 0, label,
    )
    return True


def _write_outcome_prices(signal_id: int, day_rows: list, session_scope: Callable) -> None:
    """Write OutcomePrice rows for day offsets 0..len(day_rows)-1."""
    from models import OutcomePrice

    try:
        with session_scope() as session:
            for offset, (d, high, low, close) in enumerate(day_rows):
                # Use merge to handle re-runs gracefully (upsert by PK)
                existing = session.get(OutcomePrice, (signal_id, offset))
                if existing is not None:
                    continue   # already written
                row = OutcomePrice(
                    signal_id=signal_id,
                    day_offset=offset,
                    date=d,
                    high=high,
                    low=low,
                    close=close,
                )
                session.add(row)
    except Exception:
        logger.warning("outcome_collector: outcome_prices write failed for signal_id=%d", signal_id, exc_info=True)


def _triple_barrier_label(
    entry_proxy: float,
    is_long: bool,
    highs: list[float],
    lows: list[float],
    atr_20: Optional[float],
    stop_price: Optional[float],
    target_price: Optional[float],
) -> int:
    """
    Compute triple-barrier label over the hold window.

    Barriers:
      Lower (stop): stop_price if known; else entry × (1 ∓ barrier_pct)
      Upper (target): target_price if known; else entry × (1 ± barrier_pct)
      Time: if neither barrier is hit within the available days → 0

    Returns +1, -1, or 0.
    """
    barrier_pct = _DEFAULT_BARRIER_PCT
    if atr_20 is not None and entry_proxy > 0:
        barrier_pct = max(atr_20 / entry_proxy, _DEFAULT_BARRIER_PCT)

    if is_long:
        lower = stop_price if stop_price is not None else entry_proxy * (1 - barrier_pct)
        upper = target_price if target_price is not None else entry_proxy * (1 + barrier_pct)
        for h, l in zip(highs, lows):
            if l <= lower:
                return -1
            if h >= upper:
                return +1
    else:
        lower = stop_price if stop_price is not None else entry_proxy * (1 + barrier_pct)
        upper = target_price if target_price is not None else entry_proxy * (1 - barrier_pct)
        for h, l in zip(highs, lows):
            if h >= lower:
                return -1
            if l <= upper:
                return +1

    return 0  # time barrier


def _extract_ohlc(hist) -> list[tuple]:
    """
    Convert a yfinance DataFrame to a sorted list of (date, high, low, close).

    Handles both MultiIndex (multi-ticker) and single-ticker DataFrames.
    """
    import pandas as pd

    if isinstance(hist.columns, pd.MultiIndex):
        # Multi-ticker: take the first ticker's columns
        ticker = hist.columns.get_level_values(1)[0]
        df = hist.xs(ticker, axis=1, level=1)
    else:
        df = hist

    df = df.sort_index()
    result = []
    for idx, row in df.iterrows():
        d = idx.date() if hasattr(idx, "date") else idx
        high  = float(row.get("High", row.get("high", float("nan"))))
        low   = float(row.get("Low",  row.get("low",  float("nan"))))
        close = float(row.get("Close", row.get("close", float("nan"))))
        if any(v != v for v in (high, low, close)):  # NaN check
            continue
        result.append((d, high, low, close))
    return result
