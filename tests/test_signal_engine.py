"""
Tests for signals/ — all signal components and the SignalEngine orchestrator.

Covers:
  - volume_signal: percentile rank, multi-window averaging, short-history flag
  - return_signal: absolute return rank, insufficient history guard
  - options_signal: composite, all safeguards (OI, vol, strikes, lot sizes, hedging)
  - sector_rs_signal: spread rank, neutral fallback, helper functions
  - retail_attention: component weighting, graceful degradation
  - directional_confirmation: bullish/bearish/ambiguous classification
  - compute_composite: NULL propagation, weight redistribution, retail penalty
  - SignalEngine: integration scoring with mocked adapters, signal_log write
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime
from timeutils import utcnow
from unittest.mock import MagicMock, patch
import numpy as np
import pandas as pd
import pytest

from db import init_db, reset_engine_for_testing, session_scope
from models import SignalLog, UniverseState
from signals.volume_signal import compute_volume_signal
from signals.return_signal import compute_return_signal
from signals.options_signal import compute_options_signal
from signals.sector_rs_signal import (
    compute_sector_rs_signal,
    build_spread_history,
    compute_5d_return,
)
from signals.retail_attention import compute_retail_attention
from signals.directional_confirmation import (
    AMBIGUOUS, BEARISH, BULLISH,
    compute_directional_confirmation,
)
from signals.signal_engine import SignalEngine, SignalResult, compute_composite
from universe.universe_state import TickerState


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db():
    """Fresh in-memory DB for each test."""
    reset_engine_for_testing("sqlite:///:memory:")
    init_db()
    yield
    reset_engine_for_testing("sqlite:///:memory:")


def _rng_prices(n=120, start=100.0, seed=42) -> np.ndarray:
    """Generate a synthetic daily close price series."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(0, 0.015, n)
    prices = start * np.cumprod(1 + returns)
    return prices.astype(float)


def _rng_volumes(n=120, base=1_000_000, seed=7) -> np.ndarray:
    """Generate a synthetic daily share volume series."""
    rng = np.random.default_rng(seed)
    vols = base * (1 + rng.exponential(0.5, n))
    return vols.astype(float)


def _make_engine():
    """Build a SignalEngine with fully mocked adapters."""
    config = MagicMock()
    s = MagicMock()
    s.weight_volume = 0.30
    s.weight_return = 0.25
    s.weight_options = 0.15
    s.weight_sector_rs = 0.10
    s.weight_delay = 0.10
    s.weight_floor = 0.05
    s.retail_penalty_weight = 0.20
    s.pass1_top_n = 40
    s.options_min_daily_volume = 200
    s.options_min_trade_sizes = 3
    s.iv_range_min_pct_points = 5
    s.earnings_exclusion_days_before = 1
    s.earnings_exclusion_days_after = 1

    u = MagicMock()
    u.min_options_oi = 1500
    u.min_options_strike_count = 4

    exec_cfg = MagicMock()
    exec_cfg.pass1_top_n = 40

    config.signal = s
    config.universe = u
    config.execution = exec_cfg
    config.config_hash = "testhash"

    yf = MagicMock()
    yf.get_ticker_info.return_value = None
    yf.get_daily_prices.return_value = None
    yf.get_sector_etf_prices.return_value = None

    return SignalEngine(config=config, yf_adapter=yf)


# ---------------------------------------------------------------------------
# Volume Signal
# ---------------------------------------------------------------------------

class TestVolumeSignal:
    def _history(self, n=120, base=5.0) -> np.ndarray:
        rng = np.random.default_rng(0)
        return base * (1 + rng.uniform(-0.3, 0.3, n))

    def test_high_volume_gets_high_rank(self):
        history = self._history()
        # today's vol is way above history max
        result = compute_volume_signal(today_dollar_vol=float(history.max() * 5), history_dollar_vol=history)
        assert result.composite > 0.90

    def test_low_volume_gets_low_rank(self):
        history = self._history()
        result = compute_volume_signal(today_dollar_vol=float(history.min() * 0.01), history_dollar_vol=history)
        assert result.composite < 0.10

    def test_all_three_windows_populated(self):
        history = self._history(120)
        result = compute_volume_signal(today_dollar_vol=10.0, history_dollar_vol=history)
        assert result.pctile_20d is not None
        assert result.pctile_60d is not None
        assert result.pctile_120d is not None
        assert result.short_history is False

    def test_short_history_uses_available_windows(self):
        history = self._history(70)  # 60–119 days → 20d and 60d only
        result = compute_volume_signal(today_dollar_vol=10.0, history_dollar_vol=history)
        assert result.pctile_20d is not None
        assert result.pctile_60d is not None
        assert result.pctile_120d is None
        assert result.short_history is True

    def test_very_short_history_returns_none_composite(self):
        history = self._history(10)  # < 20 days
        result = compute_volume_signal(today_dollar_vol=10.0, history_dollar_vol=history)
        assert result.composite is None

    def test_composite_is_mean_of_available_windows(self):
        history = self._history(70)  # 20d and 60d only
        result = compute_volume_signal(today_dollar_vol=7.0, history_dollar_vol=history)
        expected = np.mean([result.pctile_20d, result.pctile_60d])
        assert abs(result.composite - expected) < 1e-9


# ---------------------------------------------------------------------------
# Return Signal
# ---------------------------------------------------------------------------

class TestReturnSignal:
    def _history(self, n=80) -> np.ndarray:
        rng = np.random.default_rng(1)
        return np.abs(rng.normal(0, 0.015, n))

    def test_large_move_gets_high_rank(self):
        history = self._history()
        rank = compute_return_signal(today_abs_return=0.15, history_abs_returns=history)
        assert rank is not None and rank > 0.90

    def test_small_move_gets_low_rank(self):
        history = self._history()
        rank = compute_return_signal(today_abs_return=0.0001, history_abs_returns=history)
        assert rank is not None and rank < 0.10

    def test_insufficient_history_returns_none(self):
        history = np.abs(np.random.default_rng(2).normal(0, 0.01, 10))
        result = compute_return_signal(today_abs_return=0.02, history_abs_returns=history)
        assert result is None

    def test_output_in_unit_interval(self):
        history = self._history()
        rank = compute_return_signal(today_abs_return=0.02, history_abs_returns=history)
        assert rank is not None and 0.0 <= rank <= 1.0

    def test_uses_at_most_60d_window(self):
        """With 200 days of history, result should still be in [0,1] and use the most recent 60."""
        history = self._history(200)
        rank = compute_return_signal(today_abs_return=0.03, history_abs_returns=history)
        assert rank is not None and 0.0 <= rank <= 1.0


# ---------------------------------------------------------------------------
# Options Signal
# ---------------------------------------------------------------------------

class TestOptionsSignal:
    def _base_args(self, **overrides):
        defaults = dict(
            total_call_vol=500,
            total_put_vol=300,
            total_oi=3000,
            strike_count_with_oi=6,
            distinct_lot_sizes=5,
            current_iv=0.45,
            iv_52w_high=0.70,
            iv_52w_low=0.30,
            min_daily_volume=200,
            min_oi=1500,
            min_strikes=4,
            min_trade_sizes=3,
            iv_range_min_ppt=5.0,
        )
        defaults.update(overrides)
        return defaults

    def test_normal_case_returns_composite(self):
        result = compute_options_signal(**self._base_args())
        assert result.composite is not None
        assert 0.0 <= result.composite <= 1.0

    def test_suppressed_when_volume_below_floor(self):
        result = compute_options_signal(**self._base_args(total_call_vol=50, total_put_vol=50))
        assert result.composite is None
        assert "total_vol" in result.suppressed_reason

    def test_suppressed_when_oi_below_minimum(self):
        result = compute_options_signal(**self._base_args(total_oi=500))
        assert result.composite is None
        assert "total_oi" in result.suppressed_reason

    def test_suppressed_when_strikes_below_minimum(self):
        result = compute_options_signal(**self._base_args(strike_count_with_oi=2))
        assert result.composite is None
        assert "strike_count" in result.suppressed_reason

    def test_suppressed_when_lot_sizes_below_minimum(self):
        result = compute_options_signal(**self._base_args(distinct_lot_sizes=1))
        assert result.composite is None
        assert "lot_sizes" in result.suppressed_reason

    def test_iv_rank_set_to_neutral_when_range_insufficient(self):
        result = compute_options_signal(
            **self._base_args(iv_52w_high=0.42, iv_52w_low=0.40)  # range = 2pp < 5pp
        )
        assert result.composite is not None
        assert result.iv_range_insufficient is True
        assert result.iv_rank == 0.5

    def test_hedging_downweight_halves_composite(self):
        normal = compute_options_signal(**self._base_args())
        hedged = compute_options_signal(**self._base_args(dominant_strike_vol_pct=0.85))
        assert hedged.composite is not None
        assert hedged.hedging_downweighted is True
        # Hedged composite ≈ normal / 2 (may differ slightly due to clipping)
        assert hedged.composite < normal.composite * 0.6

    def test_defaults_to_neutral_without_iv_data(self):
        result = compute_options_signal(
            **self._base_args(current_iv=None, iv_52w_high=None, iv_52w_low=None)
        )
        assert result.composite is not None
        assert result.iv_rank == 0.5

    def test_composite_formula_weights(self):
        """No historical data → sub-components all default to 0.5."""
        result = compute_options_signal(
            **self._base_args(current_iv=None, iv_52w_high=None, iv_52w_low=None)
        )
        # pc_ratio and vol_oi default to 0.5; iv_rank = 0.5
        # composite = 0.4*0.5 + 0.3*0.5 + 0.3*0.5 = 0.5
        assert abs(result.composite - 0.5) < 0.01


# ---------------------------------------------------------------------------
# Sector RS Signal
# ---------------------------------------------------------------------------

class TestSectorRsSignal:
    def _spreads(self, n=60) -> np.ndarray:
        rng = np.random.default_rng(5)
        return rng.normal(0, 0.02, n)

    def test_high_spread_gets_high_rank(self):
        history = self._spreads()
        result = compute_sector_rs_signal(
            stock_5d_return=0.10,
            etf_5d_return=0.00,
            history_spreads=history,
        )
        assert result > 0.90

    def test_negative_spread_gets_low_rank(self):
        history = self._spreads()
        result = compute_sector_rs_signal(
            stock_5d_return=-0.10,
            etf_5d_return=0.00,
            history_spreads=history,
        )
        assert result < 0.10

    def test_neutral_when_stock_return_unavailable(self):
        history = self._spreads()
        result = compute_sector_rs_signal(None, 0.02, history)
        assert result == 0.5

    def test_neutral_when_etf_return_unavailable(self):
        history = self._spreads()
        result = compute_sector_rs_signal(0.02, None, history)
        assert result == 0.5

    def test_neutral_when_history_none(self):
        result = compute_sector_rs_signal(0.03, 0.01, None)
        assert result == 0.5

    def test_neutral_when_history_too_short(self):
        tiny_history = np.array([0.01, 0.02])  # < 5
        result = compute_sector_rs_signal(0.03, 0.01, tiny_history)
        assert result == 0.5

    def test_compute_5d_return(self):
        prices = np.array([100, 101, 102, 103, 104, 105])
        r = compute_5d_return(prices)
        assert r is not None
        assert abs(r - (105 / 100 - 1)) < 1e-9

    def test_compute_5d_return_insufficient_data(self):
        assert compute_5d_return(np.array([100, 101, 102])) is None

    def test_build_spread_history_shape(self):
        stock = _rng_prices(80)
        etf = _rng_prices(80, start=50, seed=99)
        spreads = build_spread_history(stock, etf, lookback=60)
        assert len(spreads) <= 60
        assert len(spreads) > 0


# ---------------------------------------------------------------------------
# Retail Attention
# ---------------------------------------------------------------------------

class TestRetailAttention:
    def test_maximum_attention_all_components(self):
        history = np.ones(60) * 10  # history of 10 mentions/day
        result = compute_retail_attention(
            mentions_24h=1000.0,   # way above history → max velocity
            history_mentions=history,
            scanner_flag=True,
            short_interest_pct=0.30,  # > 20%
        )
        assert result.score > 0.8

    def test_minimum_attention_all_zero(self):
        result = compute_retail_attention(
            mentions_24h=0.0,
            history_mentions=np.ones(60) * 100,  # far above today's 0 → low velocity
            scanner_flag=False,
            short_interest_pct=0.05,
        )
        assert result.score < 0.1

    def test_social_unavailable_defaults_to_zero_penalty(self):
        result = compute_retail_attention(
            mentions_24h=None,
            history_mentions=None,
            scanner_flag=False,
            short_interest_pct=None,
        )
        assert result.score == 0.0
        assert result.social_unavailable is True

    def test_si_threshold_applied(self):
        """SI just below threshold → si_crowd=0, just above → si_crowd=1."""
        below = compute_retail_attention(None, None, None, short_interest_pct=0.19)
        above = compute_retail_attention(None, None, None, short_interest_pct=0.21)
        assert below.si_crowd == 0.0
        assert above.si_crowd == 1.0

    def test_scanner_flag_weight(self):
        result_on = compute_retail_attention(None, None, True, None)
        result_off = compute_retail_attention(None, None, False, None)
        # scanner_flag contributes 0.3 × 1.0 vs 0.0
        assert abs(result_on.score - result_off.score - 0.3) < 1e-9

    def test_score_in_unit_interval(self):
        history = np.ones(60) * 5
        result = compute_retail_attention(
            mentions_24h=100.0,
            history_mentions=history,
            scanner_flag=True,
            short_interest_pct=0.30,
        )
        assert 0.0 <= result.score <= 1.0


# ---------------------------------------------------------------------------
# Directional Confirmation
# ---------------------------------------------------------------------------

class TestDirectionalConfirmation:
    def test_bullish_all_conditions_met(self):
        direction = compute_directional_confirmation(
            return_1d=0.03,
            current_price=11.0,
            prior_day_vwap=10.0,   # price above VWAP
            call_vol=1000,
            put_vol=500,           # calls > puts
        )
        assert direction == BULLISH

    def test_bearish_all_conditions_met(self):
        direction = compute_directional_confirmation(
            return_1d=-0.03,
            current_price=9.0,
            prior_day_vwap=10.0,  # price below VWAP
            call_vol=500,
            put_vol=1000,         # puts > calls
            borrow_available=True,
        )
        assert direction == BEARISH

    def test_ambiguous_when_return_and_vwap_conflict(self):
        """Positive return but price below VWAP → ambiguous."""
        direction = compute_directional_confirmation(
            return_1d=0.03,
            current_price=9.0,
            prior_day_vwap=10.0,  # price BELOW vwap despite positive return
        )
        assert direction == AMBIGUOUS

    def test_ambiguous_when_return_zero(self):
        direction = compute_directional_confirmation(
            return_1d=0.0,
            current_price=10.0,
            prior_day_vwap=10.0,
        )
        assert direction == AMBIGUOUS

    def test_bearish_blocked_when_borrow_not_available(self):
        direction = compute_directional_confirmation(
            return_1d=-0.03,
            current_price=9.0,
            prior_day_vwap=10.0,
            call_vol=500,
            put_vol=1000,
            borrow_available=False,  # hard block
        )
        assert direction == AMBIGUOUS

    def test_bearish_allowed_when_borrow_unknown(self):
        """borrow_available=None → uncertain, not blocked."""
        direction = compute_directional_confirmation(
            return_1d=-0.03,
            current_price=9.0,
            prior_day_vwap=10.0,
            put_vol=1000,
            call_vol=500,
            borrow_available=None,
        )
        assert direction == BEARISH

    def test_vwap_unavailable_not_blocking(self):
        """If no VWAP data, VWAP condition is inconclusive — don't block bullish."""
        direction = compute_directional_confirmation(
            return_1d=0.03,
            current_price=10.0,
            prior_day_vwap=None,  # unavailable
            call_vol=1000,
            put_vol=500,
        )
        assert direction == BULLISH

    def test_options_unavailable_not_blocking(self):
        """Without options data, directional check skips options filter."""
        direction = compute_directional_confirmation(
            return_1d=0.03,
            current_price=11.0,
            prior_day_vwap=10.0,
            call_vol=None,
            put_vol=None,
        )
        assert direction == BULLISH


# ---------------------------------------------------------------------------
# compute_composite (NULL propagation)
# ---------------------------------------------------------------------------

class TestComputeComposite:
    def _weights(self):
        return {
            "volume": 0.30, "return": 0.25, "options": 0.15,
            "sector_rs": 0.10, "delay": 0.10,
        }

    def test_full_signals_composite(self):
        signals = {"volume": 0.8, "return": 0.7, "options": 0.6, "sector_rs": 0.5, "delay": 0.4}
        raw, penalized, norm_w = compute_composite(signals, self._weights(), retail_penalty=0.0, penalty_weight=0.20)
        assert raw is not None
        assert abs(raw - penalized) < 1e-9  # no penalty → equal
        assert abs(sum(norm_w.values()) - 1.0) < 1e-9

    def test_null_options_redistributes_weight(self):
        """When options is None, its 0.15 weight redistributes to remaining signals."""
        signals = {"volume": 0.8, "return": 0.7, "options": None, "sector_rs": 0.5, "delay": 0.4}
        _, _, norm_w = compute_composite(signals, self._weights(), 0.0, 0.20)
        assert "options" not in norm_w
        assert abs(sum(norm_w.values()) - 1.0) < 1e-9

    def test_all_signals_none_returns_none(self):
        signals = {"volume": None, "return": None, "options": None, "sector_rs": None, "delay": None}
        raw, penalized, norm_w = compute_composite(signals, self._weights(), 0.0, 0.20)
        assert raw is None
        assert penalized is None

    def test_retail_penalty_reduces_score(self):
        signals = {"volume": 1.0, "return": 1.0, "options": 1.0, "sector_rs": 1.0, "delay": 1.0}
        _, no_penalty, _ = compute_composite(signals, self._weights(), retail_penalty=0.0, penalty_weight=0.20)
        _, with_penalty, _ = compute_composite(signals, self._weights(), retail_penalty=1.0, penalty_weight=0.20)
        # With full retail attention and penalty_weight=0.20, penalized = 1.0 × (1 - 0.20) = 0.80
        assert abs(with_penalty - 0.80) < 1e-9
        assert with_penalty < no_penalty

    def test_weight_normalisation_is_proportional(self):
        """volume=0.30, return=0.25 → if options/sector_rs/delay are null,
           volume weight should be 0.30/(0.30+0.25) ≈ 0.545."""
        signals = {"volume": 0.9, "return": 0.8, "options": None, "sector_rs": None, "delay": None}
        _, _, norm_w = compute_composite(signals, self._weights(), 0.0, 0.20)
        expected_vol = 0.30 / (0.30 + 0.25)
        assert abs(norm_w["volume"] - expected_vol) < 1e-9

    def test_composite_stays_in_unit_interval(self):
        signals = {"volume": 1.0, "return": 1.0, "options": 1.0, "sector_rs": 1.0, "delay": 1.0}
        raw, penalized, _ = compute_composite(signals, self._weights(), 0.5, 0.20)
        assert 0.0 <= raw <= 1.0
        assert 0.0 <= penalized <= 1.0


# ---------------------------------------------------------------------------
# SignalEngine integration (mocked adapters, real DB)
# ---------------------------------------------------------------------------

class TestSignalEngineIntegration:
    def _seed_active_ticker(self, ticker="SOFI"):
        with session_scope() as session:
            session.add(UniverseState(
                ticker=ticker,
                state=TickerState.ACTIVE.value,
                state_since=utcnow(),
                history_days=150,
                delay_score=0.45,
                consecutive_fail_days=0,
                consecutive_pass_days=0,
            ))

    def _make_price_df(self, tickers, n=130) -> pd.DataFrame:
        """Produce a multi-index yfinance-style DataFrame for the given tickers."""
        closes = {t: _rng_prices(n, seed=hash(t) % 1000) for t in tickers}
        volumes = {t: _rng_volumes(n, seed=hash(t) % 500) for t in tickers}
        highs = {t: closes[t] * 1.01 for t in tickers}
        lows = {t: closes[t] * 0.99 for t in tickers}

        idx = pd.bdate_range("2024-01-01", periods=n)
        tuples = []
        data = {}
        for field in ["Close", "Volume", "High", "Low"]:
            src = {"Close": closes, "Volume": volumes, "High": highs, "Low": lows}[field]
            for t in tickers:
                tuples.append((field, t))
                data[(field, t)] = pd.Series(src[t], index=idx)

        df = pd.DataFrame(data)
        df.columns = pd.MultiIndex.from_tuples(df.columns)
        return df

    def test_score_universe_returns_results_for_each_ticker(self):
        tickers = ["SOFI", "IONQ"]
        engine = _make_engine()

        df = self._make_price_df(tickers)
        engine._yf.get_daily_prices.return_value = df
        engine._yf.get_sector_etf_prices.return_value = None

        results = engine.score_universe(tickers, scan_type="midday")
        returned_tickers = {r.ticker for r in results}
        assert returned_tickers == set(tickers)

    def test_score_universe_computes_volume_signal(self):
        engine = _make_engine()
        df = self._make_price_df(["SOFI"])
        engine._yf.get_daily_prices.return_value = df

        results = engine.score_universe(["SOFI"], scan_type="midday")
        sofi = next(r for r in results if r.ticker == "SOFI")
        assert sofi.volume_pctile_20d is not None
        assert sofi.volume_pctile_60d is not None

    def test_blocked_ticker_is_excluded(self):
        from models import Override
        with session_scope() as session:
            session.add(Override(
                ticker="BADCO",
                override_type="block",
                reason="test",
                created_by="test",
                active=True,
            ))

        engine = _make_engine()
        engine._yf.get_daily_prices.return_value = None
        results = engine.score_universe(["BADCO"], scan_type="midday")
        assert len(results) == 1
        assert results[0].was_excluded is True
        assert results[0].exclusion_reason == "blocked_override"

    def test_observation_mode_flagged(self):
        """Tickers in OBSERVATION state should have is_observation_mode=True."""
        with session_scope() as session:
            session.add(UniverseState(
                ticker="NEWCO",
                state=TickerState.OBSERVATION.value,
                state_since=utcnow(),
                history_days=80,
            ))

        engine = _make_engine()
        df = self._make_price_df(["NEWCO"])
        engine._yf.get_daily_prices.return_value = df

        results = engine.score_universe(["NEWCO"], scan_type="midday")
        assert results[0].is_observation_mode is True
        assert results[0].history_status == "short"

    def test_run_scan_writes_signal_log(self):
        self._seed_active_ticker("SOFI")

        engine = _make_engine()
        df = self._make_price_df(["SOFI"])
        engine._yf.get_daily_prices.return_value = df

        engine.run_scan(scan_type="midday")

        with session_scope() as session:
            entries = session.query(SignalLog).filter(SignalLog.ticker == "SOFI").all()
        assert len(entries) == 1
        assert entries[0].scan_type == "midday"

    def test_run_scan_no_tickers_returns_empty(self):
        engine = _make_engine()
        results = engine.run_scan("midday")
        assert results == []

    def test_composite_none_when_no_price_data(self):
        """If price data is unavailable, composite_score should be None (not crash)."""
        engine = _make_engine()
        engine._yf.get_daily_prices.return_value = None

        results = engine.score_universe(["GHOST"], scan_type="midday")
        assert len(results) == 1
        assert results[0].composite_score is None

    # -- Bug 1: delay_score_as_of_signal must be persisted on signal_log --

    def test_delay_score_as_of_signal_written_when_universe_has_value(self):
        """If universe_state.delay_score is populated, signal_log row must mirror it."""
        self._seed_active_ticker("SOFI")  # seeds delay_score=0.45

        engine = _make_engine()
        df = self._make_price_df(["SOFI"])
        engine._yf.get_daily_prices.return_value = df

        engine.run_scan(scan_type="midday")

        with session_scope() as session:
            entry = session.query(SignalLog).filter(SignalLog.ticker == "SOFI").one()
        assert entry.delay_score_as_of_signal == 0.45
        assert entry.delay_score == 0.45

    def test_delay_score_as_of_signal_null_when_universe_value_null(self):
        """If universe_state has no delay_score, signal_log delay_score_as_of_signal is None."""
        with session_scope() as session:
            session.add(UniverseState(
                ticker="NODELAY",
                state=TickerState.ACTIVE.value,
                state_since=utcnow(),
                history_days=150,
                delay_score=None,  # explicitly NULL
                consecutive_fail_days=0,
                consecutive_pass_days=0,
            ))

        engine = _make_engine()
        df = self._make_price_df(["NODELAY"])
        engine._yf.get_daily_prices.return_value = df
        engine.run_scan(scan_type="midday")

        with session_scope() as session:
            entry = session.query(SignalLog).filter(SignalLog.ticker == "NODELAY").one()
        assert entry.delay_score_as_of_signal is None

    # -- Bug 2: retail_attention_score must always be numeric, never NULL --

    def test_retail_attention_score_numeric_when_no_social_adapter(self):
        """No social adapter → retail_attention_score still numeric (default 0)."""
        self._seed_active_ticker("SOFI")

        engine = _make_engine()  # no social_adapter
        df = self._make_price_df(["SOFI"])
        engine._yf.get_daily_prices.return_value = df
        # No SI data either — ticker_info=None ensures all subcomponents default
        engine._yf.get_ticker_info.return_value = None

        engine.run_scan(scan_type="midday")

        with session_scope() as session:
            entry = session.query(SignalLog).filter(SignalLog.ticker == "SOFI").one()
        assert entry.retail_attention_score is not None
        assert 0.0 <= entry.retail_attention_score <= 1.0
        assert entry.retail_attention_score == 0.0  # all subcomponents missing → 0

    def test_retail_attention_score_uses_si_when_social_adapter_missing(self):
        """When SI ≥ 20% and no social adapter, score reflects SI sub-component (0.2)."""
        self._seed_active_ticker("CROWDED")

        engine = _make_engine()  # no social adapter
        df = self._make_price_df(["CROWDED"])
        engine._yf.get_daily_prices.return_value = df

        ti = MagicMock()
        ti.market_cap_mm = 1000.0
        ti.sector_etf = "XLK"
        ti.short_pct_float = 0.30  # crowded
        engine._yf.get_ticker_info.return_value = ti

        engine.run_scan(scan_type="midday")

        with session_scope() as session:
            entry = session.query(SignalLog).filter(SignalLog.ticker == "CROWDED").one()
        # si_crowd weight = 0.2; score = 0.2 × 1.0 = 0.2
        assert entry.retail_attention_score is not None
        assert abs(entry.retail_attention_score - 0.2) < 1e-9

    def test_retail_attention_score_uses_social_adapter_when_provided(self):
        """If social adapter is wired, scanner_flag and mentions feed the score."""
        self._seed_active_ticker("HOT")

        engine = _make_engine()
        df = self._make_price_df(["HOT"])
        engine._yf.get_daily_prices.return_value = df
        engine._yf.get_ticker_info.return_value = None

        # Wire a fake social adapter
        social = MagicMock()
        sv = MagicMock()
        sv.is_trending_stocktwits = True
        sv.stocktwits_mentions_24h = 50.0
        sv.reddit_mentions_24h = 0.0
        social.get_social_velocity.return_value = sv
        engine._social = social

        engine.run_scan(scan_type="midday")

        with session_scope() as session:
            entry = session.query(SignalLog).filter(SignalLog.ticker == "HOT").one()
        # scanner flag (0.3) contributes; social_velocity has no history → 0
        assert entry.retail_attention_score is not None
        assert abs(entry.retail_attention_score - 0.3) < 1e-9

    def test_retail_attention_score_resilient_to_social_adapter_failure(self):
        """If social adapter raises, fall back to SI-only without NULL."""
        self._seed_active_ticker("FLAKY")

        engine = _make_engine()
        df = self._make_price_df(["FLAKY"])
        engine._yf.get_daily_prices.return_value = df
        engine._yf.get_ticker_info.return_value = None

        social = MagicMock()
        social.get_social_velocity.side_effect = RuntimeError("network down")
        engine._social = social

        engine.run_scan(scan_type="midday")

        with session_scope() as session:
            entry = session.query(SignalLog).filter(SignalLog.ticker == "FLAKY").one()
        assert entry.retail_attention_score is not None
        assert entry.retail_attention_score == 0.0


# ---------------------------------------------------------------------------
# Earnings exclusion uses TRADING days (F-15)
# ---------------------------------------------------------------------------

class TestEarningsExclusion:
    """_check_earnings must gate on trading-day distance, not calendar days."""

    def _engine_with_earnings(self, td_next=None, td_since=None):
        from signals.signal_engine import SignalResult
        engine = _make_engine()
        cal = MagicMock()
        cal.days_to_next_earnings.return_value = None
        cal.days_since_last_earnings.return_value = None
        cal.trading_days_to_next_earnings.return_value = td_next
        cal.trading_days_since_last_earnings.return_value = td_since
        engine._earnings = cal
        result = SignalResult(ticker="ZZZ", scan_timestamp=utcnow(), scan_type="midday")
        return engine, result

    def test_excluded_one_trading_day_before(self):
        engine, result = self._engine_with_earnings(td_next=1)   # excl_before=1
        engine._check_earnings(result)
        assert result.was_excluded is True
        assert result.earnings_proximity_tag == "excluded"

    def test_excluded_one_trading_day_after(self):
        # The Friday-reporter-on-Monday case: 1 trading day since → excluded.
        engine, result = self._engine_with_earnings(td_since=1)   # excl_after=1
        engine._check_earnings(result)
        assert result.was_excluded is True
        assert result.earnings_proximity_tag == "excluded"

    def test_post_earnings_drift_tagged_not_excluded(self):
        engine, result = self._engine_with_earnings(td_since=3)
        engine._check_earnings(result)
        assert result.was_excluded is False
        assert result.earnings_proximity_tag == "post_earnings_drift"
        assert result.catalyst_flag is True

    def test_normal_when_far_from_earnings(self):
        engine, result = self._engine_with_earnings(td_next=10, td_since=20)
        engine._check_earnings(result)
        assert result.was_excluded is False
        assert result.earnings_proximity_tag == "normal"

    def test_no_calendar_means_normal(self):
        engine = _make_engine()   # no earnings calendar wired
        from signals.signal_engine import SignalResult
        result = SignalResult(ticker="ZZZ", scan_timestamp=utcnow(), scan_type="midday")
        engine._check_earnings(result)
        assert result.was_excluded is False
        assert result.earnings_proximity_tag == "normal"
