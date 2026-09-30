"""
Cost Validator — intended vs. actual fill cost comparison.

For each traded signal, records the modeled round-trip execution cost,
decomposed into three components that SUM to the total:
  spread            = half_spread_bps × 2  (round-trip)
  impact            = slippage_bps × 2     (round-trip)
  adverse_selection = adverse_selection_bps × 2
  total             = spread + impact + adverse_selection

Flags if the rolling mean of (actual_roundtrip - working_estimate) /
working_estimate exceeds exceedance_threshold_pct over the last
exceedance_rolling_window trades.

IMPORTANT (F-10): these are MODELED costs, not measurements. The previous
implementation wrote abs(round-trip P&L) into the cost columns, so a 3% mover
recorded "300 bps of slippage" — that is the trade's *return*, not its
execution cost — and made the exceedance check a permanent false alarm. Real
execution slippage requires an intended-vs-actual fill pair, which paper
trading does not produce. Populate these columns with measured costs in Phase 4
when live fills exist; until then the decomposition reflects the cost model.

Phase 0: No trades exist, so this is a no-op that returns an empty report.

Whitepaper reference: Section 5.5 (Cost Model Validation)
Supplements reference: Doc 2, Section 2.5 (cost parameters)
"""

import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class CostReport:
    """
    Summary of the cost validation run.

    Attributes:
        n_trades_processed: Number of trades whose costs were decomposed.
        mean_actual_roundtrip_bps: Rolling mean actual round-trip cost.
        mean_working_estimate_bps: Rolling mean working estimate.
        exceedance_ratio: (actual - working) / working.
        flag_raised: True if exceedance_ratio > exceedance_threshold_pct.
        rolling_window_used: Number of trades in the rolling window.
    """
    n_trades_processed: int
    mean_actual_roundtrip_bps: Optional[float]
    mean_working_estimate_bps: Optional[float]
    exceedance_ratio: Optional[float]
    flag_raised: bool
    rolling_window_used: int


def run(
    session_scope: Callable,
    half_spread_bps: float = 10.0,
    slippage_bps: float = 5.0,
    adverse_selection_bps: float = 3.0,
    working_roundtrip_long_bps: float = 36.0,
    working_roundtrip_short_bps: float = 50.0,
    exceedance_threshold_pct: float = 0.30,
    exceedance_rolling_window: int = 20,
) -> CostReport:
    """
    Run cost decomposition for all unprocessed traded signals and check
    for rolling cost exceedance.

    Args:
        session_scope: Session context manager.
        half_spread_bps: Estimated half-spread in basis points.
        slippage_bps: Estimated one-way slippage in basis points.
        adverse_selection_bps: Estimated adverse selection per side in bps.
        working_roundtrip_long_bps: Total working estimate for a long round-trip.
        working_roundtrip_short_bps: Total working estimate for a short round-trip.
        exceedance_threshold_pct: Fraction above working estimate that triggers flag.
        exceedance_rolling_window: Number of recent trades to check.

    Returns:
        CostReport with computed metrics.
    """
    from journal.signal_log import get_traded_events_without_costs, update_cost_fields

    events = get_traded_events_without_costs(session_scope=session_scope)

    processed = 0
    for event in events:
        if event.entry_price is None or event.exit_price is None:
            continue

        decomp = _decompose_cost(
            entry_price=event.entry_price,
            exit_price=event.exit_price,
            direction=event.direction_signal or "bullish",
            half_spread_bps=half_spread_bps,
            slippage_bps=slippage_bps,
            adverse_selection_bps=adverse_selection_bps,
        )

        update_cost_fields(
            session_scope=session_scope,
            signal_id=event.id,
            slippage_bps=decomp["total_bps"],
            cost_spread_bps=decomp["spread_bps"],
            cost_impact_bps=decomp["impact_bps"],
            cost_adverse_selection_bps=decomp["adverse_selection_bps"],
        )
        processed += 1

    # Rolling exceedance check over the last N trades
    report = _check_rolling_exceedance(
        session_scope=session_scope,
        working_long=working_roundtrip_long_bps,
        working_short=working_roundtrip_short_bps,
        threshold=exceedance_threshold_pct,
        window=exceedance_rolling_window,
    )
    report.n_trades_processed = processed

    if report.flag_raised:
        logger.warning(
            "COST EXCEEDANCE: actual=%.1f bps vs working=%.1f bps "
            "(exceedance=%.1f%% > threshold=%.1f%%) over last %d trades",
            report.mean_actual_roundtrip_bps or 0,
            report.mean_working_estimate_bps or 0,
            100 * (report.exceedance_ratio or 0),
            100 * exceedance_threshold_pct,
            report.rolling_window_used,
        )

    return report


def _decompose_cost(
    entry_price: float,
    exit_price: float,
    direction: str,
    half_spread_bps: float,
    slippage_bps: float,
    adverse_selection_bps: float,
) -> dict:
    """
    Return the modeled round-trip execution cost, decomposed into components
    that sum exactly to the total:

        spread_bps            = half_spread_bps × 2
        impact_bps            = slippage_bps × 2
        adverse_selection_bps = adverse_selection_bps × 2
        total_bps             = spread_bps + impact_bps + adverse_selection_bps

    This is a modeled estimate, NOT a measurement (F-10): the trade's realised
    P&L is the strategy's return, not its execution cost, so it must never be
    written into the cost columns. entry/exit/direction are retained for the
    Phase-4 measured-fill path and for the zero-price sanity guard.

    Returns dict with keys: spread_bps, impact_bps, adverse_selection_bps, total_bps.
    """
    if entry_price is None or entry_price <= 0:
        return {"spread_bps": 0.0, "impact_bps": 0.0, "adverse_selection_bps": 0.0, "total_bps": 0.0}

    spread_rt = half_spread_bps * 2
    impact_rt = slippage_bps * 2
    adverse_rt = adverse_selection_bps * 2
    total = spread_rt + impact_rt + adverse_rt

    return {
        "spread_bps": spread_rt,
        "impact_bps": impact_rt,
        "adverse_selection_bps": adverse_rt,
        "total_bps": total,
    }


def _check_rolling_exceedance(
    session_scope: Callable,
    working_long: float,
    working_short: float,
    threshold: float,
    window: int,
) -> CostReport:
    """
    Compute rolling mean exceedance over the last `window` costed trades.
    """
    from models import SignalLog

    try:
        with session_scope() as session:
            rows = (
                session.query(SignalLog)
                .filter(
                    SignalLog.was_traded.is_(True),
                    SignalLog.slippage_bps.isnot(None),
                )
                .order_by(SignalLog.scan_timestamp.desc())
                .limit(window)
                .all()
            )
    except Exception:
        logger.error("cost_validator: rolling check query failed", exc_info=True)
        return CostReport(0, None, None, None, False, 0)

    if not rows:
        return CostReport(0, None, None, None, False, 0)

    actuals = [r.slippage_bps for r in rows if r.slippage_bps is not None]
    workings = [
        working_short if (r.direction_signal == "bearish") else working_long
        for r in rows if r.slippage_bps is not None
    ]

    if not actuals:
        return CostReport(0, None, None, None, False, 0)

    mean_actual = sum(actuals) / len(actuals)
    mean_working = sum(workings) / len(workings) if workings else 1.0
    exceedance = (mean_actual - mean_working) / max(mean_working, 1.0)
    flag = exceedance > threshold

    return CostReport(
        n_trades_processed=0,  # filled by caller
        mean_actual_roundtrip_bps=mean_actual,
        mean_working_estimate_bps=mean_working,
        exceedance_ratio=exceedance,
        flag_raised=flag,
        rolling_window_used=len(actuals),
    )
