"""Tests for universe/universe_manager.py, universe_state.py, and delay_score.py."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, datetime, timedelta
from timeutils import utcnow
from unittest.mock import MagicMock
import numpy as np
import pandas as pd
import pytest

from db import init_db, reset_engine_for_testing, session_scope
from models import UniverseState, Override
from universe.universe_state import FilterCheckResult, TickerState
from universe.delay_score import (
    compute_delay_score,
    daily_to_weekly_returns,
    compute_all_delay_scores,
)
from universe.universe_manager import (
    UniverseManager,
    _FAIL_DAYS_TO_SUSPEND,
    _PASS_DAYS_TO_REACTIVATE,
    _SUSPENSION_TO_REMOVED_DAYS,
    _MIN_DAYS_FOR_ACTIVE,
    _MIN_DAYS_FOR_CANDIDATE,
    _today_utc,
)


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db():
    """Fresh in-memory database for every test."""
    reset_engine_for_testing("sqlite:///:memory:")
    init_db()
    yield
    reset_engine_for_testing("sqlite:///:memory:")


def _make_config():
    """Return a minimal mock ConfigLoader with universe thresholds."""
    config = MagicMock()
    u = MagicMock()
    u.market_cap_min_mm = 500.0
    u.market_cap_max_mm = 2000.0
    u.min_daily_dollar_vol_mm = 5.0
    u.min_midday_dollar_vol_mm = 1.5
    u.max_spread_bps = 40.0
    u.min_options_oi = 1500
    u.min_options_strike_count = 4
    u.min_trading_days = 60
    config.universe = u
    return config


def _make_manager(history_days=150, failing_filter=None):
    """
    Create a UniverseManager with mocked adapters and a controllable
    _run_filter_checks stub.

    Args:
        history_days: History days returned for every ticker.
        failing_filter: If set, that filter check returns False; all others True.
    """
    yf = MagicMock()
    manager = UniverseManager(
        config=_make_config(),
        yf_adapter=yf,
        schwab_adapter=None,
    )

    def _stub_filter_checks(ticker, use_intraday=False):
        result = FilterCheckResult(ticker=ticker, checked_at=utcnow())
        result.market_cap_ok = True
        result.daily_dollar_vol_ok = True
        result.history_days_ok = True
        result.history_days = history_days
        if failing_filter:
            setattr(result, f"{failing_filter}_ok", False)
        return result

    manager._run_filter_checks = _stub_filter_checks
    manager._estimate_history_days = lambda ticker, result: history_days
    return manager


def _seed_ticker(
    ticker: str,
    state: TickerState,
    history_days: int = 150,
    consecutive_fail_days: int = 0,
    consecutive_pass_days: int = 0,
    suspension_reason: str = None,
    suspension_start: date = None,
):
    """Insert a UniverseState row directly into the test DB."""
    with session_scope() as session:
        record = UniverseState(
            ticker=ticker,
            state=state.value,
            state_since=utcnow(),
            history_days=history_days,
            consecutive_fail_days=consecutive_fail_days,
            consecutive_pass_days=consecutive_pass_days,
            suspension_reason=suspension_reason,
            suspension_start=suspension_start,
        )
        session.add(record)


# ---------------------------------------------------------------------------
# FilterCheckResult — unit tests
# ---------------------------------------------------------------------------

class TestFilterCheckResult:
    def test_all_pass_when_all_true(self):
        r = FilterCheckResult(ticker="X", checked_at=utcnow())
        r.market_cap_ok = True
        r.daily_dollar_vol_ok = True
        r.midday_dollar_vol_ok = True
        r.spread_ok = True
        r.options_oi_ok = True
        r.options_strike_ok = True
        r.history_days_ok = True
        assert r.all_pass is True

    def test_all_pass_when_all_none(self):
        """Inconclusive data (None) must not be counted as failure."""
        r = FilterCheckResult(ticker="X", checked_at=utcnow())
        # All checks remain None
        assert r.all_pass is True

    def test_all_pass_false_when_one_false(self):
        r = FilterCheckResult(ticker="X", checked_at=utcnow())
        r.market_cap_ok = True
        r.daily_dollar_vol_ok = False
        assert r.all_pass is False

    def test_none_does_not_block_true_checks(self):
        """Mix of True and None should still pass."""
        r = FilterCheckResult(ticker="X", checked_at=utcnow())
        r.market_cap_ok = True
        r.spread_ok = None       # data unavailable
        r.history_days_ok = True
        assert r.all_pass is True

    def test_is_inconclusive_when_all_none(self):
        """A total data outage (every check None) is inconclusive, not a pass."""
        r = FilterCheckResult(ticker="X", checked_at=utcnow())
        assert r.is_inconclusive is True
        # all_pass is vacuously True for the same row — the trap this guards.
        assert r.all_pass is True

    def test_is_inconclusive_false_when_any_check_has_data(self):
        r = FilterCheckResult(ticker="X", checked_at=utcnow())
        r.market_cap_ok = True
        assert r.is_inconclusive is False

    def test_is_inconclusive_false_when_a_check_failed(self):
        r = FilterCheckResult(ticker="X", checked_at=utcnow())
        r.spread_ok = False
        assert r.is_inconclusive is False

    def test_primary_failing_filter_returns_first_failure(self):
        r = FilterCheckResult(ticker="X", checked_at=utcnow())
        r.market_cap_ok = True
        r.daily_dollar_vol_ok = False
        r.spread_ok = False
        assert r.primary_failing_filter == "daily_dollar_vol"

    def test_primary_failing_filter_none_when_all_pass(self):
        r = FilterCheckResult(ticker="X", checked_at=utcnow())
        r.market_cap_ok = True
        assert r.primary_failing_filter is None

    def test_failure_summary_includes_failed_filter_names(self):
        r = FilterCheckResult(ticker="X", checked_at=utcnow())
        r.market_cap_ok = False
        r.market_cap_mm = 250.0
        r.spread_ok = False
        r.spread_bps = 55.0
        summary = r.failure_summary
        assert "market_cap" in summary
        assert "spread" in summary

    def test_failure_summary_all_pass_label(self):
        r = FilterCheckResult(ticker="X", checked_at=utcnow())
        assert r.failure_summary == "all pass"


# ---------------------------------------------------------------------------
# Delay score — unit tests
# ---------------------------------------------------------------------------

class TestComputeDelayScore:
    def _sync_returns(self, n=100):
        """Market and stock in perfect lockstep — delay should be near 0."""
        rng = np.random.default_rng(seed=0)
        market = rng.normal(0, 0.01, n)
        return market.copy(), market

    def _lagged_returns(self, n=120, lag=1):
        """Stock follows market with a 1-week lag — delay should be > 0."""
        rng = np.random.default_rng(seed=42)
        market = rng.normal(0, 0.01, n)
        stock = np.zeros(n)
        stock[lag:] = market[:-lag]
        stock[:lag] = 0.0
        return stock, market

    def test_perfectly_synced_stock_has_low_delay(self):
        stock, market = self._sync_returns()
        score = compute_delay_score(stock, market)
        assert score is not None
        assert score < 0.10, f"Expected low delay, got {score}"

    def test_lagged_stock_has_higher_delay(self):
        stock, market = self._lagged_returns()
        score = compute_delay_score(stock, market)
        assert score is not None
        assert score > 0.15, f"Expected higher delay for lagged stock, got {score}"

    def test_score_within_unit_interval(self):
        rng = np.random.default_rng(seed=7)
        market = rng.normal(0, 0.01, 100)
        stock = rng.normal(0, 0.01, 100)
        score = compute_delay_score(stock, market)
        assert score is None or 0.0 <= score <= 1.0

    def test_returns_none_when_insufficient_data(self):
        stock = np.array([0.01, -0.01, 0.02])
        market = np.array([0.01, -0.01, 0.02])
        assert compute_delay_score(stock, market) is None

    def test_raises_on_mismatched_lengths(self):
        with pytest.raises(ValueError, match="same length"):
            compute_delay_score(np.ones(60), np.ones(70))

    def test_returns_zero_when_full_model_explains_nothing(self):
        """Constant stock returns → r²_full = 0 → delay = 0.0 (not None)."""
        n = 80
        stock = np.zeros(n)
        rng = np.random.default_rng(seed=1)
        market = rng.normal(0, 0.01, n)
        score = compute_delay_score(stock, market)
        # If R²_full == 0, we return 0.0 per guard clause
        assert score is not None
        assert score == 0.0


class TestDailyToWeeklyReturns:
    def _make_daily_prices(self, n_weeks=30):
        """Generate a simple price series spanning n_weeks of Mon–Fri data."""
        idx = pd.bdate_range("2023-01-02", periods=n_weeks * 5)
        prices = pd.Series(
            100.0 * (1 + np.random.default_rng(seed=3).normal(0, 0.005, len(idx))).cumprod(),
            index=idx,
        )
        return prices

    def test_produces_weekly_returns(self):
        prices = self._make_daily_prices(20)
        weekly = daily_to_weekly_returns(prices)
        assert isinstance(weekly, pd.Series)
        # Weekly series should have roughly n_weeks - 1 observations (minus first week for pct_change)
        assert len(weekly) >= 15

    def test_no_nan_in_output(self):
        prices = self._make_daily_prices(20)
        weekly = daily_to_weekly_returns(prices)
        assert weekly.isna().sum() == 0

    def test_partial_week_excluded(self):
        """A week with only 2 trading days should be dropped."""
        # Jan 2 2023 is a Monday — create 6 days (Mon–Tue of week 2 only)
        idx = pd.bdate_range("2023-01-02", periods=7)  # Mon–Tue of next week
        # Manually zero out Wed–Fri by using only 7 business days → 1 full week + 2 days
        prices = pd.Series(
            [100, 101, 102, 103, 104, 105, 106],
            index=idx,
        )
        weekly = daily_to_weekly_returns(prices, min_days_per_week=3)
        # Second week has only 2 days → should be dropped → only 1 full week → pct_change gives 0 returns
        assert weekly.isna().sum() == 0


class TestComputeAllDelayScores:
    def test_returns_dict_with_all_requested_tickers(self):
        yf = MagicMock()
        # No price data → all None
        yf.get_daily_prices.return_value = None
        scores = compute_all_delay_scores(["AAPL", "SOFI"], yf)
        assert set(scores.keys()) == {"AAPL", "SOFI"}
        assert all(v is None for v in scores.values())

    def test_returns_empty_dict_for_empty_input(self):
        yf = MagicMock()
        assert compute_all_delay_scores([], yf) == {}

    def test_computes_score_from_real_data(self):
        """Build a realistic DataFrame and verify a numeric score is returned."""
        rng = np.random.default_rng(seed=99)
        n = 520  # ~2 years of trading days
        idx = pd.bdate_range("2022-01-03", periods=n)

        spy_prices = pd.Series(
            400.0 * (1 + rng.normal(0, 0.008, n)).cumprod(), index=idx
        )
        stock_prices = pd.Series(
            20.0 * (1 + rng.normal(0, 0.015, n)).cumprod(), index=idx
        )

        df = pd.DataFrame({"Close": {"SPY": spy_prices, "AAPL": stock_prices}})
        # Make it a multi-level column DataFrame like yf.download returns
        df = pd.DataFrame(
            {("Close", "SPY"): spy_prices, ("Close", "AAPL"): stock_prices},
            index=idx,
        )
        df.columns = pd.MultiIndex.from_tuples(df.columns)

        yf = MagicMock()
        yf.get_daily_prices.return_value = df

        scores = compute_all_delay_scores(["AAPL"], yf)
        assert "AAPL" in scores
        assert scores["AAPL"] is not None
        assert 0.0 <= scores["AAPL"] <= 1.0


# ---------------------------------------------------------------------------
# UniverseManager — monthly refresh
# ---------------------------------------------------------------------------

class TestMonthlyRefresh:
    def test_adds_ticker_as_observation_when_history_under_threshold(self):
        manager = _make_manager(history_days=80)  # 60–119 → OBSERVATION
        outcomes = manager.run_monthly_refresh(["SOFI"])
        assert outcomes["SOFI"] == "added_observation"

        with session_scope() as session:
            r = session.get(UniverseState, "SOFI")
        assert r is not None
        assert r.state == TickerState.OBSERVATION.value

    def test_adds_ticker_as_active_when_history_sufficient(self):
        manager = _make_manager(history_days=_MIN_DAYS_FOR_ACTIVE + 10)
        outcomes = manager.run_monthly_refresh(["MSTR"])
        assert outcomes["MSTR"] == "added_active"

        with session_scope() as session:
            r = session.get(UniverseState, "MSTR")
        assert r.state == TickerState.ACTIVE.value

    def test_skips_already_tracked_active_ticker(self):
        _seed_ticker("HOOD", TickerState.ACTIVE)
        manager = _make_manager()
        outcomes = manager.run_monthly_refresh(["HOOD"])
        assert outcomes["HOOD"] == "skipped_already_tracked"

    def test_skips_already_tracked_observation_ticker(self):
        _seed_ticker("IONQ", TickerState.OBSERVATION, history_days=90)
        manager = _make_manager()
        outcomes = manager.run_monthly_refresh(["IONQ"])
        assert outcomes["IONQ"] == "skipped_already_tracked"

    def test_skips_ticker_with_failed_filters(self):
        manager = _make_manager(failing_filter="market_cap")
        outcomes = manager.run_monthly_refresh(["TINY"])
        assert outcomes["TINY"] == "skipped_failed_filters"

    def test_skips_insufficient_history(self):
        manager = _make_manager(history_days=30)  # < _MIN_DAYS_FOR_CANDIDATE (60)
        outcomes = manager.run_monthly_refresh(["NEW"])
        assert outcomes["NEW"] == "skipped_insufficient_history"

    def test_skips_blocked_ticker(self):
        with session_scope() as session:
            session.add(Override(
                ticker="BADCO",
                override_type="block",
                reason="test block",
                created_by="test",
                active=True,
            ))
        manager = _make_manager()
        outcomes = manager.run_monthly_refresh(["BADCO"])
        assert outcomes["BADCO"] == "skipped_blocked"

    def test_re_enters_removed_ticker(self):
        _seed_ticker("BACK", TickerState.REMOVED)
        manager = _make_manager(history_days=_MIN_DAYS_FOR_ACTIVE + 20)
        outcomes = manager.run_monthly_refresh(["BACK"])
        assert outcomes["BACK"] == "added_active"

        with session_scope() as session:
            r = session.get(UniverseState, "BACK")
        assert r.state == TickerState.ACTIVE.value

    def test_handles_empty_candidate_list(self):
        manager = _make_manager()
        assert manager.run_monthly_refresh([]) == {}


# ---------------------------------------------------------------------------
# UniverseManager — daily check: ACTIVE tickers
# ---------------------------------------------------------------------------

class TestDailyCheckActiveTickers:
    def test_active_ticker_passes_stays_active(self):
        _seed_ticker("SOFI", TickerState.ACTIVE)
        manager = _make_manager()
        outcomes = manager.run_daily_check()
        assert outcomes.get("SOFI") == "passed"

        with session_scope() as session:
            r = session.get(UniverseState, "SOFI")
        assert r.state == TickerState.ACTIVE.value

    def test_active_ticker_accumulates_fail_days(self):
        _seed_ticker("SOFI", TickerState.ACTIVE, consecutive_fail_days=2,
                     suspension_reason="spread")
        manager = _make_manager(failing_filter="spread")
        outcomes = manager.run_daily_check()
        assert outcomes.get("SOFI") == "failed_filter"

        with session_scope() as session:
            r = session.get(UniverseState, "SOFI")
        assert r.consecutive_fail_days == 3
        assert r.state == TickerState.ACTIVE.value

    def test_active_ticker_suspends_after_five_consecutive_fails(self):
        """Same filter failing 5 times in a row → SUSPENDED."""
        _seed_ticker("SOFI", TickerState.ACTIVE, consecutive_fail_days=4,
                     suspension_reason="spread")
        manager = _make_manager(failing_filter="spread")
        outcomes = manager.run_daily_check()
        assert outcomes.get("SOFI") == "suspended"

        with session_scope() as session:
            r = session.get(UniverseState, "SOFI")
        assert r.state == TickerState.SUSPENDED.value
        assert r.suspension_start == _today_utc()

    def test_different_filter_each_day_resets_counter(self):
        """If the failing filter changes, counter resets to 1 — no suspension."""
        # spread has been failing for 4 days; today daily_dollar_vol fails instead
        _seed_ticker("SOFI", TickerState.ACTIVE, consecutive_fail_days=4,
                     suspension_reason="spread")
        manager = _make_manager(failing_filter="daily_dollar_vol")
        outcomes = manager.run_daily_check()
        assert outcomes.get("SOFI") == "failed_filter"

        with session_scope() as session:
            r = session.get(UniverseState, "SOFI")
        # Counter reset to 1 for new filter
        assert r.consecutive_fail_days == 1
        assert r.suspension_reason == "daily_dollar_vol"
        assert r.state == TickerState.ACTIVE.value

    def test_fail_then_pass_resets_counter(self):
        """A passing day resets the consecutive fail counter."""
        _seed_ticker("SOFI", TickerState.ACTIVE, consecutive_fail_days=3,
                     suspension_reason="spread")
        manager = _make_manager(failing_filter=None)  # all pass today
        outcomes = manager.run_daily_check()
        assert outcomes.get("SOFI") == "passed"

        with session_scope() as session:
            r = session.get(UniverseState, "SOFI")
        assert r.consecutive_fail_days == 0


# ---------------------------------------------------------------------------
# UniverseManager — daily check: OBSERVATION tickers
# ---------------------------------------------------------------------------

class TestDailyCheckObservationTickers:
    def test_observation_stays_when_history_below_threshold(self):
        _seed_ticker("IONQ", TickerState.OBSERVATION, history_days=90)
        manager = _make_manager(history_days=90)
        outcomes = manager.run_daily_check()
        assert outcomes.get("IONQ") == "passed"

        with session_scope() as session:
            r = session.get(UniverseState, "IONQ")
        assert r.state == TickerState.OBSERVATION.value

    def test_observation_transitions_to_active_at_threshold(self):
        """When history_days reaches _MIN_DAYS_FOR_ACTIVE, OBSERVATION → ACTIVE."""
        _seed_ticker("IONQ", TickerState.OBSERVATION, history_days=_MIN_DAYS_FOR_ACTIVE)
        manager = _make_manager(history_days=_MIN_DAYS_FOR_ACTIVE)
        outcomes = manager.run_daily_check()
        assert outcomes.get("IONQ") == "transitioned_to_active"

        with session_scope() as session:
            r = session.get(UniverseState, "IONQ")
        assert r.state == TickerState.ACTIVE.value


# ---------------------------------------------------------------------------
# UniverseManager — daily check: SUSPENDED tickers
# ---------------------------------------------------------------------------

class TestDailyCheckSuspendedTickers:
    def test_suspended_ticker_accumulates_pass_days(self):
        _seed_ticker("MSTR", TickerState.SUSPENDED,
                     suspension_start=_today_utc(),
                     consecutive_pass_days=1)
        manager = _make_manager()
        outcomes = manager.run_daily_check()
        assert outcomes.get("MSTR") == "suspended_recovering"

        with session_scope() as session:
            r = session.get(UniverseState, "MSTR")
        assert r.consecutive_pass_days == 2

    def test_suspended_ticker_reactivates_after_three_pass_days(self):
        _seed_ticker("MSTR", TickerState.SUSPENDED,
                     suspension_start=_today_utc(),
                     consecutive_pass_days=_PASS_DAYS_TO_REACTIVATE - 1)
        manager = _make_manager(history_days=_MIN_DAYS_FOR_ACTIVE + 10)
        outcomes = manager.run_daily_check()
        assert outcomes.get("MSTR") == "reactivated"

        with session_scope() as session:
            r = session.get(UniverseState, "MSTR")
        assert r.state == TickerState.ACTIVE.value
        assert r.suspension_reason is None
        assert r.suspension_start is None

    def test_suspended_ticker_reactivates_to_observation_if_low_history(self):
        """If history < _MIN_DAYS_FOR_ACTIVE, reactivation goes to OBSERVATION."""
        _seed_ticker("NEW", TickerState.SUSPENDED,
                     suspension_start=_today_utc(),
                     history_days=90,
                     consecutive_pass_days=_PASS_DAYS_TO_REACTIVATE - 1)
        manager = _make_manager(history_days=90)
        outcomes = manager.run_daily_check()
        assert outcomes.get("NEW") == "reactivated"

        with session_scope() as session:
            r = session.get(UniverseState, "NEW")
        assert r.state == TickerState.OBSERVATION.value

    def test_suspended_ticker_auto_removed_after_30_days(self):
        suspension_start = _today_utc() - timedelta(days=_SUSPENSION_TO_REMOVED_DAYS)
        _seed_ticker("GONE", TickerState.SUSPENDED,
                     suspension_start=suspension_start)
        manager = _make_manager()
        outcomes = manager.run_daily_check()
        assert outcomes.get("GONE") == "removed"

        with session_scope() as session:
            r = session.get(UniverseState, "GONE")
        assert r.state == TickerState.REMOVED.value

    def test_suspended_ticker_not_removed_before_30_days(self):
        suspension_start = _today_utc() - timedelta(days=_SUSPENSION_TO_REMOVED_DAYS - 1)
        _seed_ticker("HOLD", TickerState.SUSPENDED,
                     suspension_start=suspension_start,
                     consecutive_pass_days=0)
        manager = _make_manager(failing_filter="spread")
        outcomes = manager.run_daily_check()
        # Not removed — should still be dealing with filter failure
        assert outcomes.get("HOLD") != "removed"

        with session_scope() as session:
            r = session.get(UniverseState, "HOLD")
        assert r.state == TickerState.SUSPENDED.value


# ---------------------------------------------------------------------------
# UniverseManager — daily check: F-14 isolation + data-outage handling
# ---------------------------------------------------------------------------

def _inconclusive_result(ticker):
    """A FilterCheckResult with every check None (total data outage)."""
    return FilterCheckResult(ticker=ticker, checked_at=utcnow())


class TestDailyCheckOutageAndIsolation:
    def test_outage_does_not_reset_fail_counter(self):
        """An all-None (outage) day must not reset a ticker's fail streak."""
        _seed_ticker("SOFI", TickerState.ACTIVE, consecutive_fail_days=3,
                     suspension_reason="spread")
        manager = _make_manager()
        manager._run_filter_checks = lambda t, use_intraday=False: _inconclusive_result(t)
        manager._estimate_history_days = lambda t, result: None

        outcomes = manager.run_daily_check()
        assert outcomes.get("SOFI") == "inconclusive"

        with session_scope() as session:
            r = session.get(UniverseState, "SOFI")
        # Fail streak preserved, ticker unchanged.
        assert r.consecutive_fail_days == 3
        assert r.suspension_reason == "spread"
        assert r.state == TickerState.ACTIVE.value

    def test_outage_does_not_advance_reactivation(self):
        """An outage day must not advance a suspended ticker toward reactivation."""
        _seed_ticker("MSTR", TickerState.SUSPENDED,
                     suspension_start=_today_utc(),
                     consecutive_pass_days=_PASS_DAYS_TO_REACTIVATE - 1)
        manager = _make_manager()
        manager._run_filter_checks = lambda t, use_intraday=False: _inconclusive_result(t)
        manager._estimate_history_days = lambda t, result: None

        outcomes = manager.run_daily_check()
        assert outcomes.get("MSTR") == "inconclusive"

        with session_scope() as session:
            r = session.get(UniverseState, "MSTR")
        # Still suspended; pass counter not advanced.
        assert r.state == TickerState.SUSPENDED.value
        assert r.consecutive_pass_days == _PASS_DAYS_TO_REACTIVATE - 1

    def test_outage_still_allows_time_based_removal(self):
        """Date-based SUSPENDED→REMOVED still fires on an outage day."""
        suspension_start = _today_utc() - timedelta(days=_SUSPENSION_TO_REMOVED_DAYS)
        _seed_ticker("GONE", TickerState.SUSPENDED, suspension_start=suspension_start)
        manager = _make_manager()
        manager._run_filter_checks = lambda t, use_intraday=False: _inconclusive_result(t)
        manager._estimate_history_days = lambda t, result: None

        outcomes = manager.run_daily_check()
        assert outcomes.get("GONE") == "removed"

    def test_one_bad_ticker_does_not_abort_the_others(self):
        """A ticker that raises is isolated; the rest still get processed."""
        _seed_ticker("GOOD", TickerState.ACTIVE, consecutive_fail_days=2,
                     suspension_reason="spread")
        _seed_ticker("BAD", TickerState.ACTIVE)
        manager = _make_manager(failing_filter=None)  # GOOD would pass today

        good_stub = manager._run_filter_checks

        def _maybe_raise(ticker, use_intraday=False):
            if ticker == "BAD":
                raise RuntimeError("yfinance exploded")
            return good_stub(ticker, use_intraday=use_intraday)

        manager._run_filter_checks = _maybe_raise

        outcomes = manager.run_daily_check()
        assert outcomes.get("BAD") == "error"
        assert outcomes.get("GOOD") == "passed"

        with session_scope() as session:
            good = session.get(UniverseState, "GOOD")
            bad = session.get(UniverseState, "BAD")
        # GOOD's passing transition committed despite BAD raising.
        assert good.consecutive_fail_days == 0
        # BAD untouched — no partial write.
        assert bad.state == TickerState.ACTIVE.value


# ---------------------------------------------------------------------------
# UniverseManager — manual operations
# ---------------------------------------------------------------------------

class TestManualOperations:
    def test_manual_suspend(self):
        _seed_ticker("SOFI", TickerState.ACTIVE)
        manager = _make_manager()
        result = manager.suspend_ticker("SOFI", "manual test")
        assert result is True

        with session_scope() as session:
            r = session.get(UniverseState, "SOFI")
        assert r.state == TickerState.SUSPENDED.value
        assert r.suspension_reason == "manual test"
        assert r.consecutive_fail_days == _FAIL_DAYS_TO_SUSPEND

    def test_manual_suspend_nonexistent_returns_false(self):
        manager = _make_manager()
        assert manager.suspend_ticker("XXXX", "reason") is False

    def test_manual_remove(self):
        _seed_ticker("SOFI", TickerState.ACTIVE)
        manager = _make_manager()
        result = manager.remove_ticker("SOFI", "delisted")
        assert result is True

        with session_scope() as session:
            r = session.get(UniverseState, "SOFI")
        assert r.state == TickerState.REMOVED.value

    def test_manual_remove_creates_block_override(self):
        _seed_ticker("SOFI", TickerState.ACTIVE)
        manager = _make_manager()
        manager.remove_ticker("SOFI", "delisted")

        with session_scope() as session:
            override = (
                session.query(Override)
                .filter(Override.ticker == "SOFI", Override.active.is_(True))
                .first()
            )
        assert override is not None
        assert override.override_type == "block"

    def test_manual_remove_nonexistent_returns_false(self):
        manager = _make_manager()
        assert manager.remove_ticker("XXXX", "reason") is False


# ---------------------------------------------------------------------------
# UniverseManager — read accessors
# ---------------------------------------------------------------------------

class TestReadAccessors:
    def test_get_active_universe(self):
        _seed_ticker("A", TickerState.ACTIVE)
        _seed_ticker("B", TickerState.OBSERVATION)
        _seed_ticker("C", TickerState.ACTIVE)
        manager = _make_manager()
        active = manager.get_active_universe()
        assert set(active) == {"A", "C"}

    def test_get_observation_universe(self):
        _seed_ticker("X", TickerState.ACTIVE)
        _seed_ticker("Y", TickerState.OBSERVATION)
        manager = _make_manager()
        obs = manager.get_observation_universe()
        assert obs == ["Y"]

    def test_get_universe_summary_groups_by_state(self):
        _seed_ticker("A", TickerState.ACTIVE)
        _seed_ticker("B", TickerState.OBSERVATION)
        _seed_ticker("C", TickerState.SUSPENDED, suspension_start=_today_utc())
        manager = _make_manager()
        summary = manager.get_universe_summary()
        assert "A" in summary[TickerState.ACTIVE.value]
        assert "B" in summary[TickerState.OBSERVATION.value]
        assert "C" in summary[TickerState.SUSPENDED.value]
        assert summary[TickerState.REMOVED.value] == []


# ---------------------------------------------------------------------------
# UniverseManager — recompute_delay_scores (Bug 1 fix)
# ---------------------------------------------------------------------------

class TestRecomputeDelayScores:
    def test_persists_score_for_active_ticker(self, monkeypatch):
        """A NULL delay_score on an ACTIVE ticker gets populated."""
        _seed_ticker("SOFI", TickerState.ACTIVE)
        manager = _make_manager()

        def fake_compute(tickers, yf, **kwargs):
            return {t: 0.42 for t in tickers}

        monkeypatch.setattr(
            "universe.universe_manager.compute_all_delay_scores", fake_compute
        )
        manager.recompute_delay_scores(only_stale=True)

        with session_scope() as session:
            r = session.get(UniverseState, "SOFI")
        assert r.delay_score == 0.42
        assert r.delay_score_as_of == _today_utc()

    def test_persists_score_for_observation_ticker(self, monkeypatch):
        _seed_ticker("NEWCO", TickerState.OBSERVATION, history_days=80)
        manager = _make_manager()
        monkeypatch.setattr(
            "universe.universe_manager.compute_all_delay_scores",
            lambda tickers, yf, **kw: {t: 0.31 for t in tickers},
        )
        manager.recompute_delay_scores(only_stale=True)
        with session_scope() as session:
            r = session.get(UniverseState, "NEWCO")
        assert r.delay_score == 0.31

    def test_skips_suspended_and_removed_tickers(self, monkeypatch):
        _seed_ticker("OUT", TickerState.SUSPENDED, suspension_start=_today_utc())
        _seed_ticker("DEAD", TickerState.REMOVED)
        manager = _make_manager()
        called_with: list[list[str]] = []

        def spy(tickers, yf, **kw):
            called_with.append(list(tickers))
            return {t: 0.5 for t in tickers}

        monkeypatch.setattr("universe.universe_manager.compute_all_delay_scores", spy)
        manager.recompute_delay_scores(only_stale=True)
        # Neither ticker should appear in the recompute batch
        for batch in called_with:
            assert "OUT" not in batch
            assert "DEAD" not in batch

    def test_only_stale_skips_fresh_records(self, monkeypatch):
        """A ticker with a recent delay_score_as_of is left alone."""
        with session_scope() as session:
            session.add(UniverseState(
                ticker="FRESH",
                state=TickerState.ACTIVE.value,
                state_since=utcnow(),
                history_days=200,
                delay_score=0.30,
                delay_score_as_of=_today_utc() - timedelta(days=5),
                consecutive_fail_days=0,
                consecutive_pass_days=0,
            ))
        manager = _make_manager()

        called_with: list[list[str]] = []

        def spy(tickers, yf, **kw):
            called_with.append(list(tickers))
            return {t: 0.99 for t in tickers}

        monkeypatch.setattr(
            "universe.universe_manager.compute_all_delay_scores", spy
        )

        result = manager.recompute_delay_scores(only_stale=True, stale_after_days=25)
        # Fresh record means nothing to recompute
        assert result == {}
        assert called_with == []
        with session_scope() as session:
            r = session.get(UniverseState, "FRESH")
        assert r.delay_score == 0.30  # unchanged

    def test_only_stale_includes_stale_records(self, monkeypatch):
        """A ticker whose delay_score_as_of is older than threshold IS recomputed."""
        with session_scope() as session:
            session.add(UniverseState(
                ticker="OLD",
                state=TickerState.ACTIVE.value,
                state_since=utcnow(),
                history_days=200,
                delay_score=0.10,
                delay_score_as_of=_today_utc() - timedelta(days=60),
                consecutive_fail_days=0,
                consecutive_pass_days=0,
            ))
        manager = _make_manager()
        monkeypatch.setattr(
            "universe.universe_manager.compute_all_delay_scores",
            lambda tickers, yf, **kw: {t: 0.55 for t in tickers},
        )
        manager.recompute_delay_scores(only_stale=True, stale_after_days=25)

        with session_scope() as session:
            r = session.get(UniverseState, "OLD")
        assert r.delay_score == 0.55

    def test_only_stale_false_recomputes_all(self, monkeypatch):
        with session_scope() as session:
            session.add(UniverseState(
                ticker="FRESH",
                state=TickerState.ACTIVE.value,
                state_since=utcnow(),
                history_days=200,
                delay_score=0.30,
                delay_score_as_of=_today_utc(),
                consecutive_fail_days=0,
                consecutive_pass_days=0,
            ))
        manager = _make_manager()
        monkeypatch.setattr(
            "universe.universe_manager.compute_all_delay_scores",
            lambda tickers, yf, **kw: {t: 0.77 for t in tickers},
        )
        manager.recompute_delay_scores(only_stale=False)
        with session_scope() as session:
            r = session.get(UniverseState, "FRESH")
        assert r.delay_score == 0.77

    def test_none_score_does_not_overwrite_existing_value(self, monkeypatch):
        """If the recompute returns None (insufficient data), don't clobber the prior score."""
        with session_scope() as session:
            session.add(UniverseState(
                ticker="KEEP",
                state=TickerState.ACTIVE.value,
                state_since=utcnow(),
                history_days=200,
                delay_score=0.40,
                delay_score_as_of=_today_utc() - timedelta(days=60),
                consecutive_fail_days=0,
                consecutive_pass_days=0,
            ))
        manager = _make_manager()
        monkeypatch.setattr(
            "universe.universe_manager.compute_all_delay_scores",
            lambda tickers, yf, **kw: {t: None for t in tickers},
        )
        manager.recompute_delay_scores(only_stale=True, stale_after_days=25)
        with session_scope() as session:
            r = session.get(UniverseState, "KEEP")
        assert r.delay_score == 0.40  # unchanged

    def test_daily_check_triggers_recompute_for_null_scores(self, monkeypatch):
        """run_daily_check should auto-bootstrap delay_score for tickers with NULL."""
        _seed_ticker("BOOTSTRAP", TickerState.ACTIVE)
        manager = _make_manager()
        monkeypatch.setattr(
            "universe.universe_manager.compute_all_delay_scores",
            lambda tickers, yf, **kw: {t: 0.66 for t in tickers},
        )
        manager.run_daily_check()
        with session_scope() as session:
            r = session.get(UniverseState, "BOOTSTRAP")
        assert r.delay_score == 0.66
        assert r.delay_score_as_of == _today_utc()
