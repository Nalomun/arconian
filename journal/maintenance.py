"""
After-close maintenance orchestrator.

Wires together the three daily journal jobs that run at 16:15 ET:

  1. outcome_collector  — labels signal_log rows that are now >= time_stop_days
                          old (return_1d, return_3d, MAE/MFE, triple-barrier
                          outcome_label).
  2. cost_validator     — decomposes spread/impact/adverse-selection on any
                          newly-closed paper trades and raises a flag if
                          rolling cost exceeds the working estimate by more
                          than `exceedance_threshold_pct`.
  3. ic_tracker         — writes an ic_history row per (segment, component)
                          using the trailing 252-day window. Per whitepaper
                          §7.4 the formal weight-recalibration cadence is
                          every 60 trading days; running daily here just
                          keeps a continuous record and no-ops harmlessly
                          when the segment has < 5 labeled events.

Each job is wrapped in try/except so a failure in one does not skip the
others. The function returns a summary dict keyed by job name; the values
are whatever each job returned (or {"error": "..."} on failure) so the
caller can log a single concise summary line.

Whitepaper reference: §7.2 (outcome_prices), §7.4 (IC tracking), §5 (costs).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Callable

logger = logging.getLogger(__name__)

# Trailing window for IC computation, in calendar days. The whitepaper
# specifies "252 trading days" which is approximately one calendar year.
_IC_WINDOW_DAYS = 365


def run_after_close(session_scope: Callable, yf_adapter, config) -> dict:
    """
    Run the three after-close jobs. Returns a summary dict.

    Args:
        session_scope: SQLAlchemy session context manager.
        yf_adapter:    YFinanceAdapter (or compatible) for OHLC fetches.
        config:        Loaded ArconianConfig with execution / cost / signal
                       sections populated.

    Returns:
        {"outcome_collector": {...}, "cost_validator": {...}, "ic_tracker": {...}}
        Each value is the job's return dict, or {"error": str(exc)} on failure.
    """
    summary: dict = {}

    summary["outcome_collector"] = _run_outcome_collector(
        session_scope, yf_adapter, config
    )
    summary["cost_validator"] = _run_cost_validator(session_scope, config)
    summary["ic_tracker"] = _run_ic_tracker(session_scope, config)

    return summary


def _run_outcome_collector(session_scope, yf_adapter, config) -> dict:
    try:
        from journal import outcome_collector

        result = outcome_collector.run(
            session_scope=session_scope,
            yf_adapter=yf_adapter,
            time_stop_days=config.execution.time_stop_days,
        )
        logger.info(
            "maintenance: outcome_collector processed=%d skipped=%d errors=%d",
            result.get("processed", 0),
            result.get("skipped", 0),
            result.get("errors", 0),
        )
        return result
    except Exception as exc:
        logger.error("maintenance: outcome_collector failed", exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _run_cost_validator(session_scope, config) -> dict:
    try:
        from journal import cost_validator

        report = cost_validator.run(
            session_scope=session_scope,
            half_spread_bps=config.cost.half_spread_bps,
            slippage_bps=config.cost.slippage_bps,
            adverse_selection_bps=config.cost.adverse_selection_bps,
            working_roundtrip_long_bps=config.cost.working_roundtrip_long_bps,
            working_roundtrip_short_bps=config.cost.working_roundtrip_short_bps,
            exceedance_threshold_pct=config.cost.exceedance_threshold_pct,
            exceedance_rolling_window=config.cost.exceedance_rolling_window,
        )
        logger.info(
            "maintenance: cost_validator processed=%d trades, "
            "rolling_window=%d, exceedance_flag=%s",
            report.n_trades_processed,
            report.rolling_window_used,
            report.flag_raised,
        )
        return {
            "processed": report.n_trades_processed,
            "rolling_window": report.rolling_window_used,
            "flag_raised": report.flag_raised,
            "mean_actual_bps": report.mean_actual_roundtrip_bps,
            "mean_working_bps": report.mean_working_estimate_bps,
        }
    except Exception as exc:
        logger.error("maintenance: cost_validator failed", exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _run_ic_tracker(session_scope, config) -> dict:
    try:
        from journal import ic_tracker

        period_end = date.today()
        period_start = period_end - timedelta(days=_IC_WINDOW_DAYS)
        result = ic_tracker.run(
            session_scope=session_scope,
            period_start=period_start,
            period_end=period_end,
            ic_decay_threshold=config.signal.ic_decay_threshold,
            ic_consecutive_periods_for_flag=config.signal.ic_consecutive_periods_for_flag,
        )
        logger.info(
            "maintenance: ic_tracker written=%d insufficient=%d (window %s..%s)",
            result.get("written", 0),
            result.get("insufficient_data", 0),
            period_start, period_end,
        )
        return result
    except Exception as exc:
        logger.error("maintenance: ic_tracker failed", exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"}
