"""
Tests for journal/maintenance.run_after_close().

The orchestrator must:
  - call all three jobs (outcome_collector, cost_validator, ic_tracker) once
  - pass through the right config knobs to each
  - survive an exception in any one job and still call the others
  - return a dict whose keys map to each job's result (or {"error": ...})
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest


def _make_config():
    cfg = MagicMock()
    cfg.execution.time_stop_days = 3
    cfg.cost.half_spread_bps = 16.0
    cfg.cost.slippage_bps = 15.0
    cfg.cost.adverse_selection_bps = 20.0
    cfg.cost.working_roundtrip_long_bps = 120.0
    cfg.cost.working_roundtrip_short_bps = 150.0
    cfg.cost.exceedance_threshold_pct = 0.30
    cfg.cost.exceedance_rolling_window = 20
    cfg.signal.ic_decay_threshold = 0.02
    cfg.signal.ic_consecutive_periods_for_flag = 2
    return cfg


def _make_cost_report(processed=0, flag_raised=False):
    rep = MagicMock()
    rep.n_trades_processed = processed
    rep.rolling_window_used = 20
    rep.flag_raised = flag_raised
    rep.mean_actual_roundtrip_bps = 100.0
    rep.mean_working_estimate_bps = 120.0
    return rep


class TestRunAfterClose:
    def test_calls_all_three_jobs_once(self):
        from journal.maintenance import run_after_close

        sess = MagicMock()
        yf = MagicMock()
        cfg = _make_config()

        oc_result = {"processed": 5, "skipped": 2, "errors": 0}
        cv_result = _make_cost_report(processed=3)
        ic_result = {"written": 12, "insufficient_data": 30}

        with patch("journal.outcome_collector.run", return_value=oc_result) as oc_run, \
             patch("journal.cost_validator.run", return_value=cv_result) as cv_run, \
             patch("journal.ic_tracker.run", return_value=ic_result) as ic_run:

            summary = run_after_close(sess, yf, cfg)

        oc_run.assert_called_once()
        cv_run.assert_called_once()
        ic_run.assert_called_once()

        assert summary["outcome_collector"] == oc_result
        assert summary["cost_validator"]["processed"] == 3
        assert summary["cost_validator"]["flag_raised"] is False
        assert summary["ic_tracker"] == ic_result

    def test_passes_config_to_outcome_collector(self):
        from journal.maintenance import run_after_close

        sess, yf = MagicMock(), MagicMock()
        cfg = _make_config()
        cfg.execution.time_stop_days = 7  # non-default

        with patch("journal.outcome_collector.run") as oc_run, \
             patch("journal.cost_validator.run", return_value=_make_cost_report()), \
             patch("journal.ic_tracker.run", return_value={"written": 0, "insufficient_data": 0}):
            run_after_close(sess, yf, cfg)

        kwargs = oc_run.call_args.kwargs
        assert kwargs["yf_adapter"] is yf
        assert kwargs["time_stop_days"] == 7

    def test_passes_config_to_cost_validator(self):
        from journal.maintenance import run_after_close

        sess, yf = MagicMock(), MagicMock()
        cfg = _make_config()

        with patch("journal.outcome_collector.run", return_value={"processed": 0, "skipped": 0, "errors": 0}), \
             patch("journal.cost_validator.run", return_value=_make_cost_report()) as cv_run, \
             patch("journal.ic_tracker.run", return_value={"written": 0, "insufficient_data": 0}):
            run_after_close(sess, yf, cfg)

        kwargs = cv_run.call_args.kwargs
        assert kwargs["half_spread_bps"] == 16.0
        assert kwargs["working_roundtrip_long_bps"] == 120.0
        assert kwargs["exceedance_threshold_pct"] == 0.30
        assert kwargs["exceedance_rolling_window"] == 20

    def test_passes_config_to_ic_tracker(self):
        from journal.maintenance import run_after_close

        sess, yf = MagicMock(), MagicMock()
        cfg = _make_config()

        with patch("journal.outcome_collector.run", return_value={"processed": 0, "skipped": 0, "errors": 0}), \
             patch("journal.cost_validator.run", return_value=_make_cost_report()), \
             patch("journal.ic_tracker.run", return_value={"written": 0, "insufficient_data": 0}) as ic_run:
            run_after_close(sess, yf, cfg)

        kwargs = ic_run.call_args.kwargs
        assert kwargs["ic_decay_threshold"] == 0.02
        assert kwargs["ic_consecutive_periods_for_flag"] == 2
        # Trailing 365-day window
        assert kwargs["period_end"] - kwargs["period_start"] == timedelta(days=365)

    def test_outcome_collector_failure_does_not_skip_others(self):
        """A bug in outcome_collector must not prevent costs and IC from running."""
        from journal.maintenance import run_after_close

        sess, yf = MagicMock(), MagicMock()
        cfg = _make_config()

        with patch("journal.outcome_collector.run", side_effect=RuntimeError("boom")), \
             patch("journal.cost_validator.run", return_value=_make_cost_report(processed=2)) as cv_run, \
             patch("journal.ic_tracker.run", return_value={"written": 1, "insufficient_data": 0}) as ic_run:
            summary = run_after_close(sess, yf, cfg)

        cv_run.assert_called_once()
        ic_run.assert_called_once()
        assert "error" in summary["outcome_collector"]
        assert "RuntimeError" in summary["outcome_collector"]["error"]
        assert summary["cost_validator"]["processed"] == 2
        assert summary["ic_tracker"]["written"] == 1

    def test_cost_validator_failure_does_not_skip_ic(self):
        from journal.maintenance import run_after_close

        sess, yf = MagicMock(), MagicMock()
        cfg = _make_config()

        with patch("journal.outcome_collector.run", return_value={"processed": 0, "skipped": 0, "errors": 0}), \
             patch("journal.cost_validator.run", side_effect=ValueError("schema mismatch")), \
             patch("journal.ic_tracker.run", return_value={"written": 7, "insufficient_data": 5}) as ic_run:
            summary = run_after_close(sess, yf, cfg)

        ic_run.assert_called_once()
        assert "error" in summary["cost_validator"]
        assert "ValueError" in summary["cost_validator"]["error"]
        assert summary["ic_tracker"]["written"] == 7

    def test_ic_tracker_failure_returns_error(self):
        from journal.maintenance import run_after_close

        sess, yf = MagicMock(), MagicMock()
        cfg = _make_config()

        with patch("journal.outcome_collector.run", return_value={"processed": 0, "skipped": 0, "errors": 0}), \
             patch("journal.cost_validator.run", return_value=_make_cost_report()), \
             patch("journal.ic_tracker.run", side_effect=Exception("scipy missing")):
            summary = run_after_close(sess, yf, cfg)

        assert "error" in summary["ic_tracker"]
        assert "scipy missing" in summary["ic_tracker"]["error"]

    def test_summary_structure_when_all_succeed(self):
        """The summary dict must always have the three expected keys."""
        from journal.maintenance import run_after_close

        sess, yf = MagicMock(), MagicMock()
        cfg = _make_config()

        with patch("journal.outcome_collector.run", return_value={"processed": 0, "skipped": 0, "errors": 0}), \
             patch("journal.cost_validator.run", return_value=_make_cost_report()), \
             patch("journal.ic_tracker.run", return_value={"written": 0, "insufficient_data": 0}):
            summary = run_after_close(sess, yf, cfg)

        assert set(summary.keys()) == {"outcome_collector", "cost_validator", "ic_tracker"}
