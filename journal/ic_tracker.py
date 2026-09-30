"""
Information Coefficient tracker.

Computes Spearman rank correlation between composite_score and 3-day forward
return, segmented by scan type and retail attention regime. Writes results
to ic_history and sets decay_flag when IC falls below threshold for
ic_consecutive_periods_for_flag consecutive periods.

Called from the after-close maintenance job every ic_recalibration_period_days.

Whitepaper reference: Section 3.3 (IC Monitoring)
"""

import logging
from datetime import date, datetime, timedelta
from timeutils import utcnow
from typing import Callable, Optional

import numpy as np
from scipy import stats as scipy_stats

logger = logging.getLogger(__name__)

# Segments computed for every IC run
_SEGMENTS = [
    "all",
    "open_scan",
    "midday_scan",
    "preclose_scan",
    "high_retail",
    "low_retail",
    "post_earnings_drift",
    "normal",
]

_COMPONENTS = ["composite", "volume", "return", "options", "sector_rs", "delay"]


def compute_spearman_ic(scores: list[float], returns: list[float]) -> Optional[float]:
    """
    Compute Spearman rank correlation between signal scores and forward returns.

    Args:
        scores: List of composite (or component) signal scores.
        returns: List of corresponding 3-day forward returns.

    Returns:
        Spearman r value, or None if fewer than 5 paired observations.
    """
    if len(scores) < 5 or len(returns) < 5:
        return None
    r, _ = scipy_stats.spearmanr(scores, returns)
    return float(r) if not np.isnan(r) else None


def run(
    session_scope: Callable,
    period_start: date,
    period_end: date,
    ic_decay_threshold: float = 0.02,
    ic_consecutive_periods_for_flag: int = 2,
) -> dict:
    """
    Compute IC for all segments and components over the given period and
    write results to ic_history.

    Args:
        session_scope: Session context manager.
        period_start: Start of the evaluation period.
        period_end: End of the evaluation period.
        ic_decay_threshold: IC below this value triggers a decay flag.
        ic_consecutive_periods_for_flag: Consecutive periods below threshold
            before decay_flag is set to True.

    Returns:
        Summary dict: {written, skipped_insufficient_data}.
    """
    from journal.signal_log import get_events_for_ic

    written = insufficient = 0

    for segment in _SEGMENTS:
        events = get_events_for_ic(
            session_scope=session_scope,
            period_start=period_start,
            period_end=period_end,
            segment=segment,
        )

        if not events:
            insufficient += len(_COMPONENTS)
            continue

        for component in _COMPONENTS:
            scores, returns = _extract_scores_returns(events, component)
            ic_value = compute_spearman_ic(scores, returns)
            n = len(scores)

            if n < 5:
                insufficient += 1
                continue

            decay = _check_decay(
                session_scope=session_scope,
                component=component,
                segment=segment,
                ic_value=ic_value,
                threshold=ic_decay_threshold,
                consecutive_periods=ic_consecutive_periods_for_flag,
            )

            _write_ic_row(
                session_scope=session_scope,
                period_start=period_start,
                period_end=period_end,
                component=component,
                segment=segment,
                ic_value=ic_value,
                ic_count=n,
                decay_flag=decay,
            )
            written += 1

    logger.info(
        "ic_tracker: written=%d insufficient=%d (period %s – %s)",
        written, insufficient, period_start, period_end,
    )
    return {"written": written, "insufficient_data": insufficient}


def get_recent_ic(
    session_scope: Callable,
    component: str = "composite",
    segment: str = "all",
    n_periods: int = 5,
) -> list[dict]:
    """
    Return the n most recent IC values for a given component/segment.

    Args:
        session_scope: Session context manager.
        component: Signal component name.
        segment: Segment name.
        n_periods: Number of periods to return.

    Returns:
        List of dicts with keys: computed_at, period_start, period_end,
        ic_value, ic_count, decay_flag.
    """
    from models import ICHistory

    try:
        with session_scope() as session:
            rows = (
                session.query(ICHistory)
                .filter_by(signal_component=component, segment=segment)
                .order_by(ICHistory.computed_at.desc())
                .limit(n_periods)
                .all()
            )
            result = [
                {
                    "computed_at": r.computed_at,
                    "period_start": r.period_start,
                    "period_end": r.period_end,
                    "ic_value": r.ic_value,
                    "ic_count": r.ic_count,
                    "decay_flag": r.decay_flag,
                }
                for r in rows
            ]
        return result
    except Exception:
        logger.error("ic_tracker.get_recent_ic: query failed", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_scores_returns(events: list, component: str) -> tuple[list, list]:
    """
    Extract parallel (score, return_3d) lists from signal log rows.

    Args:
        events: List of SignalLog ORM rows.
        component: Which score to extract ('composite', 'volume', etc.).

    Returns:
        (scores, returns) — both lists with NaN/None pairs dropped. The return
        is direction-adjusted (see below).

    Direction convention (F-25): ``return_3d`` is stored raw (long convention),
    but a high signal score on a *bearish* call should be rewarded when the
    price falls. We therefore flip the return's sign for rows whose
    ``direction_signal`` is ``'bearish'`` so that, for every row, a favorable
    move in the signal's predicted direction is a positive number. Without
    this, bearish rows correlate score against the wrong-signed return and
    attenuate measured IC toward zero — biasing the system's go/no-go metric.
    ``'bullish'``, ``'ambiguous'``, and unlabeled (NULL) rows keep the raw
    long convention.
    """
    _field_map = {
        "composite":  "composite_score",
        "volume":     "volume_pctile_20d",
        "return":     "return_pctile_60d",
        "options":    "options_composite",
        "sector_rs":  "sector_rs_percentile",
        "delay":      "delay_score",
    }
    field = _field_map.get(component, "composite_score")

    scores, returns = [], []
    for ev in events:
        score = getattr(ev, field, None)
        ret   = ev.return_3d
        if score is None or ret is None:
            continue
        if getattr(ev, "direction_signal", None) == "bearish":
            ret = -ret
        scores.append(float(score))
        returns.append(float(ret))
    return scores, returns


def _check_decay(
    session_scope: Callable,
    component: str,
    segment: str,
    ic_value: Optional[float],
    threshold: float,
    consecutive_periods: int,
) -> bool:
    """
    Return True if the last `consecutive_periods` IC values (including the
    current one) are all below `threshold`.
    """
    from models import ICHistory

    if ic_value is None or ic_value >= threshold:
        return False

    # Count how many of the last (consecutive_periods - 1) rows are also below threshold
    try:
        with session_scope() as session:
            prior = (
                session.query(ICHistory)
                .filter_by(signal_component=component, segment=segment)
                .order_by(ICHistory.computed_at.desc())
                .limit(consecutive_periods - 1)
                .all()
            )
    except Exception:
        return False

    if len(prior) < consecutive_periods - 1:
        return False  # not enough history yet

    return all(
        r.ic_value is not None and r.ic_value < threshold
        for r in prior
    )


def _write_ic_row(
    session_scope: Callable,
    period_start: date,
    period_end: date,
    component: str,
    segment: str,
    ic_value: Optional[float],
    ic_count: int,
    decay_flag: bool,
) -> None:
    """Insert or update an ic_history row for the given period/component/segment."""
    from models import ICHistory
    from sqlalchemy.exc import IntegrityError

    now = utcnow()
    try:
        with session_scope() as session:
            # Check for existing row (unique on computed_at + component + segment)
            # Use period_start as the computed_at key to make runs idempotent.
            computed_at = datetime(period_start.year, period_start.month, period_start.day)
            existing = (
                session.query(ICHistory)
                .filter_by(
                    computed_at=computed_at,
                    signal_component=component,
                    segment=segment,
                )
                .first()
            )
            if existing is not None:
                existing.ic_value = ic_value
                existing.ic_count = ic_count
                existing.decay_flag = decay_flag
            else:
                row = ICHistory(
                    computed_at=computed_at,
                    period_start=period_start,
                    period_end=period_end,
                    signal_component=component,
                    segment=segment,
                    ic_value=ic_value,
                    ic_count=ic_count,
                    decay_flag=decay_flag,
                )
                session.add(row)

        if decay_flag:
            logger.warning(
                "IC DECAY FLAG: component=%s segment=%s ic=%.4f (threshold crossed for %d periods)",
                component, segment, ic_value or 0, 0,
            )
    except IntegrityError:
        logger.warning(
            "ic_tracker: duplicate row skipped (component=%s segment=%s period=%s)",
            component, segment, period_start,
        )
    except Exception:
        logger.error("ic_tracker: write failed", exc_info=True)
