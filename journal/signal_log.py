"""
Signal log read-side API.

The write path is handled by signals/signal_engine.py (_write_signal_log).
This module provides the query interface used by the outcome collector,
IC tracker, and cost validator.

Whitepaper reference: Section 7.1 (signal_log schema)
"""

import logging
from datetime import datetime, date
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def get_unresolved_events(
    session_scope: Callable,
    min_age_days: int = 3,
    limit: int = 500,
    max_age_days: Optional[int] = 30,
) -> list:
    """
    Return signal_log rows where return_3d is NULL and the signal is old
    enough for outcome prices to have settled.

    Args:
        session_scope: Session context manager.
        min_age_days: Minimum days since scan before outcome collection runs.
            Default 3 matches the time_stop_days hold window.
        limit: Maximum rows returned per call (prevents runaway queries).
        max_age_days: Upper age bound (F-24). Rows older than this whose outcome
            still cannot be computed (e.g. delisted/halted tickers that will never
            produce 4 bars) are abandoned: excluded from the queue so they can't
            permanently clog the ASC-ordered head and starve newer rows. The
            queue returns oldest-first, so without this bound a handful of dead
            rows would sit at the front forever. None disables the bound.

    Returns:
        List of SignalLog ORM rows (detached from session).
    """
    from models import SignalLog
    from sqlalchemy import func

    try:
        with session_scope() as session:
            q = session.query(SignalLog).filter(
                SignalLog.return_3d.is_(None),
                SignalLog.composite_score.isnot(None),  # scored events only
                func.julianday("now") - func.julianday(SignalLog.scan_timestamp) >= min_age_days,
            )
            if max_age_days is not None:
                q = q.filter(
                    func.julianday("now") - func.julianday(SignalLog.scan_timestamp) <= max_age_days
                )
            rows = q.order_by(SignalLog.scan_timestamp.asc()).limit(limit).all()
            for row in rows:
                session.expunge(row)
        return rows
    except Exception:
        logger.error("signal_log.get_unresolved_events: query failed", exc_info=True)
        return []


def count_abandoned_events(session_scope: Callable, max_age_days: int = 30) -> int:
    """
    Count unresolved (return_3d NULL) scored rows older than max_age_days — the
    dead-letter set that get_unresolved_events has stopped retrying (F-24).
    """
    from models import SignalLog
    from sqlalchemy import func

    try:
        with session_scope() as session:
            return (
                session.query(SignalLog)
                .filter(
                    SignalLog.return_3d.is_(None),
                    SignalLog.composite_score.isnot(None),
                    func.julianday("now") - func.julianday(SignalLog.scan_timestamp) > max_age_days,
                )
                .count()
            )
    except Exception:
        logger.error("signal_log.count_abandoned_events: query failed", exc_info=True)
        return 0


def update_outcomes(
    session_scope: Callable,
    signal_id: int,
    return_1d: Optional[float] = None,
    return_3d: Optional[float] = None,
    return_from_signal_time_3d: Optional[float] = None,
    max_adverse_excursion: Optional[float] = None,
    max_favorable_excursion: Optional[float] = None,
    outcome_label: Optional[int] = None,
) -> bool:
    """
    Write outcome fields back to a signal_log row.

    Args:
        session_scope: Session context manager.
        signal_id: Primary key of the SignalLog row.
        return_1d: 1-day forward return (close-to-close, fraction).
        return_3d: 3-day forward return.
        return_from_signal_time_3d: Signal-time-to-signal-time 3d return.
        max_adverse_excursion: Max drawdown during hold window (negative fraction).
        max_favorable_excursion: Max gain during hold window (positive fraction).
        outcome_label: +1 upper barrier, -1 lower barrier, 0 time barrier.

    Returns:
        True if the row was updated, False on error or not found.
    """
    from models import SignalLog

    try:
        with session_scope() as session:
            row = session.get(SignalLog, signal_id)
            if row is None:
                logger.warning("signal_log.update_outcomes: signal_id %d not found", signal_id)
                return False

            if return_1d is not None:
                row.return_1d = return_1d
            if return_3d is not None:
                row.return_3d = return_3d
            if return_from_signal_time_3d is not None:
                row.return_from_signal_time_3d = return_from_signal_time_3d
            if max_adverse_excursion is not None:
                row.max_adverse_excursion = max_adverse_excursion
            if max_favorable_excursion is not None:
                row.max_favorable_excursion = max_favorable_excursion
            if outcome_label is not None:
                row.outcome_label = outcome_label

        return True
    except Exception:
        logger.error("signal_log.update_outcomes: update failed for id=%d", signal_id, exc_info=True)
        return False


def get_events_for_ic(
    session_scope: Callable,
    period_start: date,
    period_end: date,
    segment: str = "all",
    min_score: Optional[float] = None,
) -> list:
    """
    Return signal_log rows with both composite_score and return_3d populated,
    within the given date range, for IC computation.

    Args:
        session_scope: Session context manager.
        period_start: Inclusive start date (matches scan_timestamp date).
        period_end: Inclusive end date.
        segment: Filter by segment. Supported values:
            'all'           — no segment filter
            'open_scan'     — scan_type contains 'morning' or 'open'
            'midday_scan'   — scan_type='midday'
            'preclose_scan' — scan_type contains 'afternoon' or 'preclose'
            'high_retail'        — retail_attention_score > 0.5
            'low_retail'         — retail_attention_score <= 0.5
            'post_earnings_drift' — earnings_proximity_tag='post_earnings_drift'
            'normal'             — earnings_proximity_tag='normal'
        min_score: Optional minimum composite_score filter (excludes noise).

    Returns:
        List of detached SignalLog rows.
    """
    from models import SignalLog

    start_dt = datetime(period_start.year, period_start.month, period_start.day)
    end_dt = datetime(period_end.year, period_end.month, period_end.day, 23, 59, 59)

    try:
        with session_scope() as session:
            q = (
                session.query(SignalLog)
                .filter(
                    SignalLog.composite_score.isnot(None),
                    SignalLog.return_3d.isnot(None),
                    SignalLog.scan_timestamp >= start_dt,
                    SignalLog.scan_timestamp <= end_dt,
                )
            )

            if min_score is not None:
                q = q.filter(SignalLog.composite_score >= min_score)

            # Segment filters
            if segment == "open_scan":
                q = q.filter(SignalLog.scan_type == "open")
            elif segment == "midday_scan":
                q = q.filter(SignalLog.scan_type == "midday")
            elif segment == "preclose_scan":
                q = q.filter(SignalLog.scan_type == "preclose")
            elif segment == "high_retail":
                q = q.filter(SignalLog.retail_attention_score > 0.5)
            elif segment == "low_retail":
                q = q.filter(
                    SignalLog.retail_attention_score.isnot(None),
                    SignalLog.retail_attention_score <= 0.5,
                )
            elif segment == "post_earnings_drift":
                q = q.filter(SignalLog.earnings_proximity_tag == "post_earnings_drift")
            elif segment == "normal":
                q = q.filter(SignalLog.earnings_proximity_tag == "normal")

            rows = q.order_by(SignalLog.scan_timestamp.asc()).all()
            for row in rows:
                session.expunge(row)
        return rows
    except Exception:
        logger.error("signal_log.get_events_for_ic: query failed", exc_info=True)
        return []


def get_traded_events_without_costs(session_scope: Callable, limit: int = 200) -> list:
    """
    Return traded signal_log rows where slippage_bps has not yet been filled.

    Used by cost_validator to find trades pending cost decomposition.

    Args:
        session_scope: Session context manager.
        limit: Max rows per call.

    Returns:
        List of detached SignalLog rows where was_traded=True and slippage_bps IS NULL.
    """
    from models import SignalLog

    try:
        with session_scope() as session:
            rows = (
                session.query(SignalLog)
                .filter(
                    SignalLog.was_traded.is_(True),
                    SignalLog.slippage_bps.is_(None),
                    SignalLog.entry_price.isnot(None),
                    SignalLog.exit_price.isnot(None),
                )
                .order_by(SignalLog.scan_timestamp.asc())
                .limit(limit)
                .all()
            )
            for row in rows:
                session.expunge(row)
        return rows
    except Exception:
        logger.error("signal_log.get_traded_events_without_costs: query failed", exc_info=True)
        return []


def update_cost_fields(
    session_scope: Callable,
    signal_id: int,
    slippage_bps: Optional[float] = None,
    cost_spread_bps: Optional[float] = None,
    cost_impact_bps: Optional[float] = None,
    cost_adverse_selection_bps: Optional[float] = None,
    cost_borrow_bps: Optional[float] = None,
) -> bool:
    """
    Write cost decomposition fields to a signal_log row.

    Returns True if written, False on error.
    """
    from models import SignalLog

    try:
        with session_scope() as session:
            row = session.get(SignalLog, signal_id)
            if row is None:
                return False
            if slippage_bps is not None:
                row.slippage_bps = slippage_bps
            if cost_spread_bps is not None:
                row.cost_spread_bps = cost_spread_bps
            if cost_impact_bps is not None:
                row.cost_impact_bps = cost_impact_bps
            if cost_adverse_selection_bps is not None:
                row.cost_adverse_selection_bps = cost_adverse_selection_bps
            if cost_borrow_bps is not None:
                row.cost_borrow_bps = cost_borrow_bps
        return True
    except Exception:
        logger.error("signal_log.update_cost_fields: failed for id=%d", signal_id, exc_info=True)
        return False
