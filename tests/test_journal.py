"""
Tests for journal/ modules:
  - parameter_tracker: baseline write, diff detection, weight updates
  - signal_log: get_unresolved_events, update_outcomes, get_events_for_ic,
                get_traded_events_without_costs, update_cost_fields
  - outcome_collector: triple_barrier_label, _decompose_cost (via cost_validator)
  - ic_tracker: compute_spearman_ic, run, get_recent_ic
  - cost_validator: _decompose_cost, CostReport structure, run (Phase 0 no-op)
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
from datetime import date, datetime, timedelta
from timeutils import utcnow
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from db import init_db, reset_engine_for_testing, session_scope
from models import ICHistory, ParameterHistory, SignalLog


# ---------------------------------------------------------------------------
# Shared DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db():
    reset_engine_for_testing("sqlite:///:memory:")
    init_db()
    yield
    reset_engine_for_testing("sqlite:///:memory:")


def _seed_signal_log(
    ticker="AAPL",
    scan_type="midday",
    composite_score=0.75,
    return_3d=None,
    retail_attention_score=0.6,
    earnings_proximity_tag="normal",
    was_traded=False,
    entry_price=None,
    exit_price=None,
    slippage_bps=None,
    direction_signal="bullish",
    atr_20=1.0,
    days_ago=4,
) -> int:
    """Insert a SignalLog row and return its id."""
    scan_ts = utcnow() - timedelta(days=days_ago)
    with session_scope() as session:
        row = SignalLog(
            scan_timestamp=scan_ts,
            scan_type=scan_type,
            ticker=ticker,
            composite_score=composite_score,
            return_3d=return_3d,
            retail_attention_score=retail_attention_score,
            earnings_proximity_tag=earnings_proximity_tag,
            was_traded=was_traded,
            entry_price=entry_price,
            exit_price=exit_price,
            slippage_bps=slippage_bps,
            direction_signal=direction_signal,
            atr_20=atr_20,
            history_status="full",
            corporate_action_flag=False,
            catalyst_flag=False,
        )
        session.add(row)
        session.flush()
        sid = row.id
    return sid


# ---------------------------------------------------------------------------
# parameter_tracker
# ---------------------------------------------------------------------------

class TestParameterTracker:

    def test_baseline_written_on_first_run(self):
        from journal.parameter_tracker import record_config_snapshot

        config = MagicMock()
        config.config_hash = "abc123"
        config.raw = {"signal": {"weight_volume": 0.30}}
        config.diff = MagicMock(return_value=[])

        # Simulate first run (no prior history)
        record_config_snapshot(config, session_scope)

        with session_scope() as session:
            rows = session.query(ParameterHistory).all()
        assert len(rows) == 1
        assert rows[0].parameter_path == "_baseline"
        assert rows[0].triggered_by == "startup"

    def test_no_changes_returns_zero(self):
        from journal.parameter_tracker import record_config_snapshot, _write_baseline

        config = MagicMock()
        config.config_hash = "abc123"
        config.raw = {"signal": {"weight_volume": 0.30}}
        config.diff = MagicMock(return_value=[])

        # Write baseline first
        _write_baseline(config, session_scope, utcnow())

        n = record_config_snapshot(config, session_scope)
        assert n == 0

    def test_changed_params_written(self):
        from journal.parameter_tracker import record_config_snapshot, _write_baseline

        config = MagicMock()
        config.config_hash = "abc123"
        config.raw = {"signal": {"weight_volume": 0.30}}
        config.diff = MagicMock(return_value=[
            ("signal.weight_volume", 0.25, 0.30),
            ("signal.weight_return", 0.20, 0.25),
        ])

        _write_baseline(config, session_scope, utcnow())
        n = record_config_snapshot(config, session_scope)
        assert n == 2

        with session_scope() as session:
            rows = session.query(ParameterHistory).filter_by(triggered_by="startup").all()
        paths = {r.parameter_path for r in rows}
        assert "signal.weight_volume" in paths
        assert "signal.weight_return" in paths

    def test_record_weight_update(self):
        from journal.parameter_tracker import record_weight_update

        old = {"volume": 0.30, "return": 0.25}
        new = {"volume": 0.28, "return": 0.27}
        n = record_weight_update(old, new, "IC period 1", session_scope)
        assert n == 2

        with session_scope() as session:
            rows = session.query(ParameterHistory).filter_by(triggered_by="ic_recalibration").all()
        assert len(rows) == 2

    def test_record_weight_update_no_change(self):
        from journal.parameter_tracker import record_weight_update

        weights = {"volume": 0.30}
        n = record_weight_update(weights, weights, "period", session_scope)
        assert n == 0

    def test_serialise_handles_non_json(self):
        from journal.parameter_tracker import _serialise
        assert isinstance(_serialise(None), str)
        assert isinstance(_serialise([1, 2, 3]), str)
        assert isinstance(_serialise({"a": 1}), str)

    def test_change_then_restart_does_not_self_corrupt(self):
        """F-23: after a real config change, the next startup must diff against a
        full-config baseline — never a scalar change row (which produced a
        garbage parameter_path='' row and re-diffed the whole config)."""
        from journal.parameter_tracker import record_config_snapshot
        from config.config_loader import ConfigLoader

        class _FakeConfig:
            def __init__(self, raw):
                self.raw = raw
                self.config_hash = repr(raw)

            def diff(self, other_raw):
                changes: list = []
                ConfigLoader._diff_recursive(other_raw, self.raw, "", changes)
                return changes

        cfg_a = _FakeConfig({"signal": {"weight_volume": 0.30}})
        cfg_b = _FakeConfig({"signal": {"weight_volume": 0.40}})

        # Startup 1: first run writes the baseline.
        assert record_config_snapshot(cfg_a, session_scope) == 0
        # Startup 2: weight changed → exactly one change row written.
        assert record_config_snapshot(cfg_b, session_scope) == 1
        # Startup 3: no further change → clean no-op (the regression: old code
        # diffed the full config against the scalar "0.40" change row here).
        assert record_config_snapshot(cfg_b, session_scope) == 0

        with session_scope() as session:
            rows = session.query(ParameterHistory).all()
        paths = [r.parameter_path for r in rows]
        # No garbage empty-path row ever created.
        assert "" not in paths
        # The real change was recorded once.
        assert paths.count("signal.weight_volume") == 1


# ---------------------------------------------------------------------------
# signal_log read API
# ---------------------------------------------------------------------------

class TestSignalLogRead:

    def test_get_unresolved_events_returns_old_events(self):
        from journal.signal_log import get_unresolved_events

        _seed_signal_log(days_ago=4, return_3d=None, composite_score=0.7)
        rows = get_unresolved_events(session_scope, min_age_days=3)
        assert len(rows) == 1

    def test_get_unresolved_events_excludes_recent(self):
        from journal.signal_log import get_unresolved_events

        _seed_signal_log(days_ago=1, return_3d=None, composite_score=0.7)
        rows = get_unresolved_events(session_scope, min_age_days=3)
        assert len(rows) == 0

    def test_get_unresolved_events_excludes_already_resolved(self):
        from journal.signal_log import get_unresolved_events

        _seed_signal_log(days_ago=5, return_3d=0.02, composite_score=0.7)
        rows = get_unresolved_events(session_scope, min_age_days=3)
        assert len(rows) == 0

    def test_get_unresolved_events_excludes_unscored(self):
        from journal.signal_log import get_unresolved_events

        _seed_signal_log(days_ago=5, return_3d=None, composite_score=None)
        rows = get_unresolved_events(session_scope, min_age_days=3)
        assert len(rows) == 0

    def test_update_outcomes_writes_fields(self):
        from journal.signal_log import update_outcomes

        sid = _seed_signal_log(days_ago=5)
        ok = update_outcomes(
            session_scope, sid,
            return_1d=0.01, return_3d=0.03,
            max_adverse_excursion=-0.02, max_favorable_excursion=0.04,
            outcome_label=1,
        )
        assert ok is True

        with session_scope() as session:
            row = session.get(SignalLog, sid)
            assert abs(row.return_3d - 0.03) < 1e-9
            assert row.outcome_label == 1
            assert abs(row.max_adverse_excursion - (-0.02)) < 1e-9

    def test_update_outcomes_missing_id_returns_false(self):
        from journal.signal_log import update_outcomes

        ok = update_outcomes(session_scope, signal_id=99999, return_3d=0.01)
        assert ok is False

    def test_update_outcomes_partial_update(self):
        from journal.signal_log import update_outcomes

        sid = _seed_signal_log(days_ago=5)
        update_outcomes(session_scope, sid, return_1d=0.01)

        with session_scope() as session:
            row = session.get(SignalLog, sid)
        assert abs(row.return_1d - 0.01) < 1e-9
        assert row.return_3d is None  # not set

    def test_get_events_for_ic_returns_rows_with_return(self):
        from journal.signal_log import get_events_for_ic

        sid = _seed_signal_log(days_ago=5, return_3d=0.02, composite_score=0.8)

        start = date.today() - timedelta(days=10)
        end = date.today()
        rows = get_events_for_ic(session_scope, start, end)
        assert len(rows) == 1
        assert rows[0].id == sid

    def test_get_events_for_ic_excludes_null_return(self):
        from journal.signal_log import get_events_for_ic

        _seed_signal_log(days_ago=5, return_3d=None)
        rows = get_events_for_ic(session_scope, date.today() - timedelta(days=10), date.today())
        assert len(rows) == 0

    def test_get_events_for_ic_segment_high_retail(self):
        from journal.signal_log import get_events_for_ic

        _seed_signal_log(ticker="A", days_ago=5, return_3d=0.01, retail_attention_score=0.8)
        _seed_signal_log(ticker="B", days_ago=5, return_3d=0.01, retail_attention_score=0.3)

        rows = get_events_for_ic(
            session_scope, date.today() - timedelta(10), date.today(), segment="high_retail"
        )
        assert len(rows) == 1
        assert rows[0].ticker == "A"

    def test_get_events_for_ic_segment_low_retail(self):
        from journal.signal_log import get_events_for_ic

        _seed_signal_log(ticker="A", days_ago=5, return_3d=0.01, retail_attention_score=0.8)
        _seed_signal_log(ticker="B", days_ago=5, return_3d=0.01, retail_attention_score=0.3)

        rows = get_events_for_ic(
            session_scope, date.today() - timedelta(10), date.today(), segment="low_retail"
        )
        assert len(rows) == 1
        assert rows[0].ticker == "B"

    def test_get_events_for_ic_segment_midday(self):
        from journal.signal_log import get_events_for_ic

        _seed_signal_log(ticker="A", scan_type="midday", days_ago=5, return_3d=0.01)
        _seed_signal_log(ticker="B", scan_type="open", days_ago=5, return_3d=0.01)

        rows = get_events_for_ic(
            session_scope, date.today() - timedelta(10), date.today(), segment="midday_scan"
        )
        assert len(rows) == 1
        assert rows[0].ticker == "A"

    def test_get_events_for_ic_segment_post_earnings_drift(self):
        """F-25: the segment formerly mislabeled 'pre_earnings' is now
        'post_earnings_drift' and selects exactly those rows."""
        from journal.signal_log import get_events_for_ic

        _seed_signal_log(ticker="A", days_ago=5, return_3d=0.01,
                         earnings_proximity_tag="post_earnings_drift")
        _seed_signal_log(ticker="B", days_ago=5, return_3d=0.01,
                         earnings_proximity_tag="normal")

        rows = get_events_for_ic(
            session_scope, date.today() - timedelta(10), date.today(),
            segment="post_earnings_drift",
        )
        assert len(rows) == 1
        assert rows[0].ticker == "A"

    def test_get_traded_events_without_costs(self):
        from journal.signal_log import get_traded_events_without_costs

        _seed_signal_log(was_traded=True, entry_price=50.0, exit_price=51.0, slippage_bps=None)
        _seed_signal_log(was_traded=True, entry_price=50.0, exit_price=51.0, slippage_bps=10.0)
        _seed_signal_log(was_traded=False)

        rows = get_traded_events_without_costs(session_scope)
        assert len(rows) == 1

    def test_update_cost_fields_writes(self):
        from journal.signal_log import update_cost_fields

        sid = _seed_signal_log()
        ok = update_cost_fields(
            session_scope, sid,
            slippage_bps=15.0, cost_spread_bps=8.0,
            cost_impact_bps=5.0, cost_adverse_selection_bps=2.0,
        )
        assert ok is True

        with session_scope() as session:
            row = session.get(SignalLog, sid)
        assert abs(row.slippage_bps - 15.0) < 1e-9
        assert abs(row.cost_spread_bps - 8.0) < 1e-9


# ---------------------------------------------------------------------------
# ic_tracker
# ---------------------------------------------------------------------------

class TestICTracker:

    def test_compute_spearman_ic_positive_correlation(self):
        from journal.ic_tracker import compute_spearman_ic

        scores  = [0.1, 0.3, 0.5, 0.7, 0.9, 0.4, 0.6, 0.2, 0.8, 1.0]
        returns = [0.01, 0.02, 0.03, 0.04, 0.05, 0.02, 0.03, 0.01, 0.04, 0.06]
        ic = compute_spearman_ic(scores, returns)
        assert ic is not None
        assert ic > 0.8  # near-perfect rank correlation

    def test_compute_spearman_ic_insufficient_data(self):
        from journal.ic_tracker import compute_spearman_ic

        ic = compute_spearman_ic([0.5, 0.6], [0.01, 0.02])
        assert ic is None

    def test_compute_spearman_ic_zero_correlation(self):
        from journal.ic_tracker import compute_spearman_ic

        rng = np.random.default_rng(42)
        scores  = rng.uniform(0, 1, 50).tolist()
        returns = rng.uniform(-0.05, 0.05, 50).tolist()
        ic = compute_spearman_ic(scores, returns)
        assert ic is not None
        assert abs(ic) < 0.3   # near zero

    def test_extract_scores_returns_flips_bearish_sign(self):
        """F-25: bearish rows have their forward return sign-flipped so a
        correct bearish call (high score, price fell) is a positive number."""
        from journal.ic_tracker import _extract_scores_returns

        bullish = _seed_signal_log(ticker="BULL", days_ago=5, composite_score=0.8,
                                   return_3d=0.02, direction_signal="bullish")
        bearish = _seed_signal_log(ticker="BEAR", days_ago=5, composite_score=0.8,
                                   return_3d=-0.03, direction_signal="bearish")
        ambiguous = _seed_signal_log(ticker="AMB", days_ago=5, composite_score=0.5,
                                     return_3d=-0.01, direction_signal="ambiguous")

        with session_scope() as session:
            events = [session.get(SignalLog, sid)
                      for sid in (bullish, bearish, ambiguous)]
            for ev in events:
                session.expunge(ev)

        scores, returns = _extract_scores_returns(events, "composite")
        by_score = dict(zip([e.ticker for e in events], returns))
        # Bullish kept raw; bearish flipped (-0.03 -> +0.03); ambiguous raw.
        assert by_score["BULL"] == pytest.approx(0.02)
        assert by_score["BEAR"] == pytest.approx(0.03)
        assert by_score["AMB"] == pytest.approx(-0.01)

    def test_run_writes_ic_history(self):
        from journal.ic_tracker import run

        # Seed 10 events with scores and returns
        for i in range(10):
            _seed_signal_log(
                ticker=f"T{i}",
                composite_score=0.1 * (i + 1),
                return_3d=0.005 * (i + 1),
                days_ago=10,
            )

        start = date.today() - timedelta(days=20)
        end = date.today() - timedelta(days=1)
        result = run(session_scope, start, end)
        assert result["written"] > 0

        with session_scope() as session:
            count = session.query(ICHistory).count()
        assert count > 0

    def test_run_sets_decay_flag_when_ic_below_threshold(self):
        from journal.ic_tracker import run, _write_ic_row

        # Pre-seed a prior period with low IC
        prior_start = date.today() - timedelta(days=60)
        prior_end = date.today() - timedelta(days=31)
        _write_ic_row(
            session_scope=session_scope,
            period_start=prior_start,
            period_end=prior_end,
            component="composite",
            segment="all",
            ic_value=0.005,    # below decay threshold of 0.02
            ic_count=20,
            decay_flag=False,
        )

        # Seed data that will also produce low IC
        rng = np.random.default_rng(0)
        for i in range(10):
            _seed_signal_log(
                ticker=f"T{i}",
                composite_score=rng.uniform(0, 1),
                return_3d=rng.uniform(-0.05, 0.05),
                days_ago=10,
            )

        start = date.today() - timedelta(days=20)
        end = date.today() - timedelta(days=1)
        # Set a very high threshold so essentially everything is below it
        run(session_scope, start, end, ic_decay_threshold=10.0, ic_consecutive_periods_for_flag=2)

        with session_scope() as session:
            flagged = session.query(ICHistory).filter_by(decay_flag=True).count()
        assert flagged > 0

    def test_run_no_data_returns_zero_written(self):
        from journal.ic_tracker import run

        start = date.today() - timedelta(days=20)
        end = date.today() - timedelta(days=15)
        result = run(session_scope, start, end)
        assert result["written"] == 0

    def test_get_recent_ic_returns_sorted(self):
        from journal.ic_tracker import get_recent_ic, _write_ic_row

        for i in range(3):
            _write_ic_row(
                session_scope=session_scope,
                period_start=date.today() - timedelta(days=60 * (i + 1)),
                period_end=date.today() - timedelta(days=60 * i + 1),
                component="composite",
                segment="all",
                ic_value=0.05 + 0.01 * i,
                ic_count=30,
                decay_flag=False,
            )

        recent = get_recent_ic(session_scope, n_periods=2)
        assert len(recent) == 2
        # Most recent first
        assert recent[0]["computed_at"] >= recent[1]["computed_at"]

    def test_get_recent_ic_empty_returns_empty(self):
        from journal.ic_tracker import get_recent_ic

        result = get_recent_ic(session_scope)
        assert result == []


# ---------------------------------------------------------------------------
# cost_validator
# ---------------------------------------------------------------------------

class TestCostValidator:

    def test_decompose_cost_long(self):
        from journal.cost_validator import _decompose_cost

        result = _decompose_cost(
            entry_price=50.0, exit_price=51.0,
            direction="bullish",
            half_spread_bps=10, slippage_bps=5, adverse_selection_bps=3,
        )
        assert result["total_bps"] > 0
        assert result["spread_bps"] == 20.0  # 2× half-spread
        assert result["impact_bps"] == 10.0  # 2× slippage
        assert "adverse_selection_bps" in result

    def test_decompose_components_sum_to_total(self):
        # F-10: components must sum to the total, and the total must be the
        # MODELED cost — not the trade's P&L magnitude (here ~200 bps).
        from journal.cost_validator import _decompose_cost
        r = _decompose_cost(
            entry_price=50.0, exit_price=51.0,   # +200 bps return
            direction="bullish",
            half_spread_bps=16, slippage_bps=15, adverse_selection_bps=20,
        )
        assert r["spread_bps"] + r["impact_bps"] + r["adverse_selection_bps"] == r["total_bps"]
        assert r["total_bps"] == 102.0          # 32 + 30 + 40, not 200
        assert r["total_bps"] != 200.0

    def test_decompose_cost_zero_entry(self):
        from journal.cost_validator import _decompose_cost

        result = _decompose_cost(
            entry_price=0.0, exit_price=1.0,
            direction="bullish",
            half_spread_bps=10, slippage_bps=5, adverse_selection_bps=3,
        )
        assert result["total_bps"] == 0

    def test_decompose_cost_short(self):
        from journal.cost_validator import _decompose_cost

        # Short: profit from price falling
        result = _decompose_cost(
            entry_price=50.0, exit_price=49.0,
            direction="bearish",
            half_spread_bps=10, slippage_bps=5, adverse_selection_bps=3,
        )
        assert result["total_bps"] > 0

    def test_run_no_trades_returns_zero_processed(self):
        from journal.cost_validator import run

        report = run(session_scope)
        assert report.n_trades_processed == 0
        assert report.flag_raised is False

    def test_run_with_traded_event_processes(self):
        from journal.cost_validator import run

        _seed_signal_log(
            was_traded=True,
            entry_price=50.0, exit_price=50.5,
            slippage_bps=None,
            direction_signal="bullish",
        )

        report = run(session_scope, half_spread_bps=10, slippage_bps=5, adverse_selection_bps=3)
        assert report.n_trades_processed == 1

    def test_cost_report_dataclass(self):
        from journal.cost_validator import CostReport

        r = CostReport(
            n_trades_processed=5,
            mean_actual_roundtrip_bps=40.0,
            mean_working_estimate_bps=36.0,
            exceedance_ratio=0.11,
            flag_raised=False,
            rolling_window_used=5,
        )
        assert r.n_trades_processed == 5
        assert r.flag_raised is False

    def test_exceedance_flag_raised_when_over_threshold(self):
        """If actual costs >> working, flag_raised=True."""
        from journal.cost_validator import run

        # Seed 5 trades with very high slippage
        for i in range(5):
            sid = _seed_signal_log(
                ticker=f"T{i}",
                was_traded=True,
                entry_price=50.0, exit_price=50.5,
                slippage_bps=200.0,  # already has slippage — won't be reprocessed
                direction_signal="bullish",
            )

        # working_roundtrip_long = 36 bps, actual = 200 bps → exceedance ~ 456%
        report = run(
            session_scope,
            working_roundtrip_long_bps=36.0,
            exceedance_threshold_pct=0.30,
            exceedance_rolling_window=10,
        )
        assert report.flag_raised is True


# ---------------------------------------------------------------------------
# outcome_collector — triple barrier label
# ---------------------------------------------------------------------------

class TestTripleBarrierLabel:

    def test_upper_barrier_hit_long(self):
        from journal.outcome_collector import _triple_barrier_label

        label = _triple_barrier_label(
            entry_proxy=100.0, is_long=True,
            highs=[105.0, 103.0], lows=[99.0, 98.0],
            atr_20=2.0, stop_price=97.0, target_price=104.0,
        )
        assert label == 1

    def test_lower_barrier_hit_long(self):
        from journal.outcome_collector import _triple_barrier_label

        label = _triple_barrier_label(
            entry_proxy=100.0, is_long=True,
            highs=[101.0, 102.0], lows=[96.0, 95.0],
            atr_20=2.0, stop_price=97.0, target_price=106.0,
        )
        assert label == -1

    def test_time_barrier_long(self):
        from journal.outcome_collector import _triple_barrier_label

        label = _triple_barrier_label(
            entry_proxy=100.0, is_long=True,
            highs=[101.0, 102.0], lows=[99.0, 98.5],
            atr_20=2.0, stop_price=95.0, target_price=110.0,
        )
        assert label == 0

    def test_upper_barrier_hit_short(self):
        from journal.outcome_collector import _triple_barrier_label

        # Short: wins when price falls; upper = price below target
        label = _triple_barrier_label(
            entry_proxy=100.0, is_long=False,
            highs=[101.0, 100.5], lows=[94.0, 95.0],
            atr_20=2.0, stop_price=103.0, target_price=95.0,
        )
        assert label == 1

    def test_default_barriers_used_when_no_explicit_stops(self):
        from journal.outcome_collector import _triple_barrier_label

        # With default 3% barrier: upper=103, lower=97
        label = _triple_barrier_label(
            entry_proxy=100.0, is_long=True,
            highs=[104.0], lows=[99.0],
            atr_20=None, stop_price=None, target_price=None,
        )
        assert label == 1

    def test_extract_ohlc_single_ticker(self):
        from journal.outcome_collector import _extract_ohlc
        import pandas as pd

        dates = pd.date_range("2025-01-01", periods=3)
        df = pd.DataFrame({
            "High":  [10.5, 11.0, 11.5],
            "Low":   [9.5, 10.0, 10.5],
            "Close": [10.0, 10.5, 11.0],
        }, index=dates)
        rows = _extract_ohlc(df)
        assert len(rows) == 3
        assert rows[0][3] == 10.0   # close day 0
        assert rows[2][2] == 10.5   # low day 2


# ---------------------------------------------------------------------------
# outcome_collector._process_event — F-6 (stop source) and F-11 (window/MAE-MFE)
# ---------------------------------------------------------------------------

def _mock_yf_hist(bars):
    """bars: list of (date, high, low, close) → mock yf_adapter."""
    import pandas as pd
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, *_ in bars])
    df = pd.DataFrame(
        {"High": [h for _, h, l, c in bars],
         "Low":  [l for _, h, l, c in bars],
         "Close": [c for _, h, l, c in bars]},
        index=idx,
    )
    yf = MagicMock()
    yf.get_price_history.return_value = df
    return yf


def _detached_event(sid):
    with session_scope() as s:
        ev = s.get(SignalLog, sid)
        s.expunge(ev)
    return ev


class TestProcessEvent:

    def test_full_window_resolves_and_writes_outcomes(self):
        from journal.outcome_collector import _process_event
        sid = _seed_signal_log(ticker="AAA", direction_signal="bullish", atr_20=1.0)
        ev = _detached_event(sid)
        d0 = ev.scan_timestamp.date()
        bars = [(d0, 101, 99, 100),
                (d0 + timedelta(days=1), 102, 100, 101),
                (d0 + timedelta(days=2), 103, 101, 102),
                (d0 + timedelta(days=3), 104, 102, 103)]
        assert _process_event(ev, session_scope, _mock_yf_hist(bars)) is True
        with session_scope() as s:
            row = s.get(SignalLog, sid)
            assert row.return_3d is not None
            assert row.outcome_label in (-1, 0, 1)

    def test_insufficient_bars_defers_without_labeling(self):
        # F-11: fewer than 4 bars must defer, not write a premature label.
        from journal.outcome_collector import _process_event
        sid = _seed_signal_log(ticker="BBB")
        ev = _detached_event(sid)
        d0 = ev.scan_timestamp.date()
        bars = [(d0, 101, 99, 100),
                (d0 + timedelta(days=1), 102, 100, 101),
                (d0 + timedelta(days=2), 103, 101, 102)]
        assert _process_event(ev, session_scope, _mock_yf_hist(bars)) is False
        with session_scope() as s:
            assert s.get(SignalLog, sid).outcome_label is None

    def test_traded_row_uses_trade_stop_not_entry_price(self):
        # F-6: a long that dips below entry (100) but stays above the real stop
        # (90) and rallies must label +1. The old code used entry_price as the
        # stop → low 95 <= 100 → wrongly -1.
        from journal.outcome_collector import _process_event
        from models import Trade
        with session_scope() as s:
            t = Trade(ticker="CCC", direction="long", status="open",
                      entry_price=100.0, stop_price=90.0, atr_at_entry=2.0)
            s.add(t); s.flush(); tid = t.id
        sid = _seed_signal_log(ticker="CCC", direction_signal="bullish",
                               atr_20=1.0, was_traded=True, entry_price=100.0)
        with session_scope() as s:
            s.get(SignalLog, sid).trade_id = tid
        ev = _detached_event(sid)
        d0 = ev.scan_timestamp.date()
        bars = [(d0, 101, 99, 100),
                (d0 + timedelta(days=1), 110, 95, 108),
                (d0 + timedelta(days=2), 111, 107, 109),
                (d0 + timedelta(days=3), 112, 108, 110)]
        _process_event(ev, session_scope, _mock_yf_hist(bars))
        with session_scope() as s:
            assert s.get(SignalLog, sid).outcome_label == 1

    def test_day0_intraday_excluded_from_excursions(self):
        # F-11: entry proxy is the day-0 close, so day-0's intraday spike must
        # not count toward MFE.
        from journal.outcome_collector import _process_event
        sid = _seed_signal_log(ticker="DDD", direction_signal="bullish", atr_20=1.0)
        ev = _detached_event(sid)
        d0 = ev.scan_timestamp.date()
        bars = [(d0, 200, 99, 100),                       # day0 spikes to 200
                (d0 + timedelta(days=1), 100.5, 99.5, 100),
                (d0 + timedelta(days=2), 100.5, 99.5, 100),
                (d0 + timedelta(days=3), 100.5, 99.5, 100)]
        _process_event(ev, session_scope, _mock_yf_hist(bars))
        with session_scope() as s:
            row = s.get(SignalLog, sid)
            assert row.max_favorable_excursion < 0.1   # ~0.005, not 1.0


# ---------------------------------------------------------------------------
# Outcome-queue dead-letter (F-24): aged-out rows must not clog the queue
# ---------------------------------------------------------------------------

class TestOutcomeQueueDeadLetter:

    def test_unresolved_excludes_aged_out_rows(self):
        from journal.signal_log import get_unresolved_events
        fresh = _seed_signal_log(ticker="FRESH", days_ago=5, return_3d=None)
        old = _seed_signal_log(ticker="OLD", days_ago=45, return_3d=None)
        ids = {e.id for e in get_unresolved_events(session_scope, min_age_days=3, max_age_days=30)}
        assert fresh in ids
        assert old not in ids   # abandoned — would otherwise clog the ASC head

    def test_count_abandoned_events(self):
        from journal.signal_log import count_abandoned_events
        _seed_signal_log(ticker="OLD1", days_ago=45, return_3d=None)
        _seed_signal_log(ticker="OLD2", days_ago=60, return_3d=None)
        _seed_signal_log(ticker="FRESH", days_ago=5, return_3d=None)
        assert count_abandoned_events(session_scope, max_age_days=30) == 2

    def test_max_age_none_disables_bound(self):
        from journal.signal_log import get_unresolved_events
        old = _seed_signal_log(ticker="OLD", days_ago=45, return_3d=None)
        ids = {e.id for e in get_unresolved_events(session_scope, min_age_days=3, max_age_days=None)}
        assert old in ids
