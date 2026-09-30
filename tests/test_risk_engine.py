"""
Tests for risk/risk_engine.py and supporting risk modules.

Covers:
  - check_regime: all three regime levels and boundary conditions
  - RiskEngine.current_regime: config-driven thresholds
  - RiskEngine.size_position: share sizing, catalyst premium, caps, edge cases
  - RiskEngine.approve_new_position: full gate — CB block, crisis, sizing, constraints
  - RiskEngine integration: CB multiplier flows through to sizing
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import math
from datetime import date, timedelta
from unittest.mock import MagicMock

import numpy as np
import pytest

from risk.risk_engine import (
    RegimeLevel,
    RiskEngine,
    PositionSpec,
    check_regime,
)
from risk.circuit_breakers import CircuitBreakerManager
from risk.portfolio_constraints import PortfolioConstraintChecker, PositionContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_risk_config(
    phase1_risk=0.01,        # 1% of equity per trade
    atr_stop_mult=1.5,
    catalyst_premium=1.5,   # premium FACTOR (×base); effective catalyst mult = 1.5×1.5 = 2.25
    max_position_pct=0.10,
    max_concurrent=5,
    max_sector_capital_pct=0.30,
    max_correlated_cluster=3,
    correlation_threshold=0.6,
    max_overnight_pct=0.60,
    vix_elevated=25.0,
    vix_crisis=35.0,
    iwm_elevated_10d=-0.05,
    iwm_crisis_20d=-0.10,
    tp1_r_multiple=2.0,
) -> MagicMock:
    r = MagicMock()
    r.phase1_risk_per_trade = phase1_risk
    r.base_risk_per_trade = phase1_risk
    r.atr_stop_multiplier = atr_stop_mult
    r.catalyst_atr_premium = catalyst_premium
    r.max_position_pct = max_position_pct
    r.max_concurrent_positions = max_concurrent
    r.max_sector_capital_pct = max_sector_capital_pct
    r.max_correlated_cluster = max_correlated_cluster
    r.correlation_threshold = correlation_threshold
    r.max_overhead_exposure_pct = max_overnight_pct
    r.max_overnight_exposure_pct = max_overnight_pct
    r.vix_elevated_threshold = vix_elevated
    r.vix_crisis_threshold = vix_crisis
    r.iwm_elevated_10d_return = iwm_elevated_10d
    r.iwm_crisis_20d_return = iwm_crisis_20d
    return r


def _make_config(
    phase1_risk=0.01,
    atr_stop_mult=1.5,
    catalyst_premium=1.5,   # premium FACTOR (×base); effective catalyst mult = 1.5×1.5 = 2.25
    max_position_pct=0.10,
    max_concurrent=5,
    tp1_r_multiple=2.0,
    **kwargs,
) -> MagicMock:
    config = MagicMock()
    config.risk = _make_risk_config(
        phase1_risk=phase1_risk,
        atr_stop_mult=atr_stop_mult,
        catalyst_premium=catalyst_premium,
        max_position_pct=max_position_pct,
        max_concurrent=max_concurrent,
        tp1_r_multiple=tp1_r_multiple,
        **kwargs,
    )
    config.execution.tp1_r_multiple = tp1_r_multiple  # tp1_r_multiple lives in execution config
    return config


def _make_engine(
    phase1_risk=0.01,
    atr_stop_mult=1.5,
    catalyst_premium=1.5,   # premium FACTOR (×base); effective catalyst mult = 1.5×1.5 = 2.25
    max_position_pct=0.10,
    max_concurrent=5,
    tp1_r_multiple=2.0,
    cb_manager=None,
    **kwargs,
) -> RiskEngine:
    config = _make_config(
        phase1_risk=phase1_risk,
        atr_stop_mult=atr_stop_mult,
        catalyst_premium=catalyst_premium,
        max_position_pct=max_position_pct,
        max_concurrent=max_concurrent,
        tp1_r_multiple=tp1_r_multiple,
        **kwargs,
    )
    cb = cb_manager or CircuitBreakerManager(config_max_positions=max_concurrent)
    constraints = PortfolioConstraintChecker(
        max_positions=max_concurrent,
        max_sub_industry=2,
        max_sector_capital_pct=config.risk.max_sector_capital_pct,
        max_correlated_cluster=config.risk.max_correlated_cluster,
        correlation_threshold=config.risk.correlation_threshold,
        max_overnight_exposure_pct=config.risk.max_overnight_exposure_pct,
    )
    return RiskEngine(config=config, cb_manager=cb, constraint_checker=constraints)


# ---------------------------------------------------------------------------
# check_regime (standalone function)
# ---------------------------------------------------------------------------

class TestCheckRegime:

    def test_normal_regime_default_thresholds(self):
        lvl = check_regime(vix=20.0, iwm_10d_return=0.01, iwm_20d_return=0.02)
        assert lvl == RegimeLevel.NORMAL

    def test_elevated_by_vix(self):
        lvl = check_regime(vix=26.0, iwm_10d_return=0.00, iwm_20d_return=0.00)
        assert lvl == RegimeLevel.ELEVATED

    def test_elevated_by_iwm_10d(self):
        lvl = check_regime(vix=20.0, iwm_10d_return=-0.06, iwm_20d_return=0.00)
        assert lvl == RegimeLevel.ELEVATED

    def test_crisis_by_vix(self):
        lvl = check_regime(vix=36.0, iwm_10d_return=0.00, iwm_20d_return=0.00)
        assert lvl == RegimeLevel.CRISIS

    def test_crisis_by_iwm_20d(self):
        lvl = check_regime(vix=20.0, iwm_10d_return=0.00, iwm_20d_return=-0.11)
        assert lvl == RegimeLevel.CRISIS

    def test_crisis_takes_precedence_over_elevated(self):
        """VIX=40 > both thresholds → must return CRISIS, not ELEVATED."""
        lvl = check_regime(vix=40.0, iwm_10d_return=-0.07, iwm_20d_return=-0.12)
        assert lvl == RegimeLevel.CRISIS

    def test_vix_at_elevated_boundary(self):
        """VIX exactly at elevated threshold triggers Elevated (> not >=)."""
        lvl = check_regime(vix=25.0, iwm_10d_return=0.0, iwm_20d_return=0.0)
        assert lvl == RegimeLevel.NORMAL   # 25.0 is NOT > 25.0

    def test_vix_just_above_elevated_boundary(self):
        lvl = check_regime(vix=25.001, iwm_10d_return=0.0, iwm_20d_return=0.0)
        assert lvl == RegimeLevel.ELEVATED

    def test_custom_thresholds(self):
        lvl = check_regime(
            vix=30.0,
            iwm_10d_return=-0.04,
            iwm_20d_return=-0.08,
            vix_elevated_threshold=30.0,
            vix_crisis_threshold=40.0,
            iwm_elevated_10d=-0.05,
            iwm_crisis_20d=-0.10,
        )
        # vix=30 is NOT > 30, iwm_10d=-0.04 is NOT < -0.05, iwm_20d not crisis
        assert lvl == RegimeLevel.NORMAL

    def test_iwm_20d_exactly_at_crisis_boundary(self):
        """IWM 20d return exactly at -10% is NOT < -10% → not crisis."""
        lvl = check_regime(vix=20.0, iwm_10d_return=0.0, iwm_20d_return=-0.10)
        assert lvl == RegimeLevel.NORMAL


# ---------------------------------------------------------------------------
# RiskEngine.current_regime
# ---------------------------------------------------------------------------

class TestRiskEnginCurrentRegime:

    def test_uses_config_thresholds(self):
        engine = _make_engine(vix_elevated=30.0, vix_crisis=45.0)
        # VIX=28 < 30 → NORMAL with custom thresholds
        assert engine.current_regime(28.0, 0.0, 0.0) == RegimeLevel.NORMAL

    def test_elevated_via_custom_vix(self):
        engine = _make_engine(vix_elevated=30.0, vix_crisis=45.0)
        assert engine.current_regime(31.0, 0.0, 0.0) == RegimeLevel.ELEVATED

    def test_crisis_via_custom_vix(self):
        engine = _make_engine(vix_elevated=30.0, vix_crisis=45.0)
        assert engine.current_regime(46.0, 0.0, 0.0) == RegimeLevel.CRISIS


# ---------------------------------------------------------------------------
# RiskEngine.size_position
# ---------------------------------------------------------------------------

class TestSizePosition:
    """
    Default setup: equity=100_000, phase1_risk=1% → risk=$1000.
    atr_stop_mult=1.5, catalyst_premium=1.5 (premium factor → catalyst stop_dist=2.25).
    entry_price=10, atr_20=1.0 (stop_dist=1.5 for non-catalyst).
    Expected shares = floor(1000 / 1.5) = 666.
    Position value = 666*10 = $6,660 = 6.7% of equity → below 10% cap.
    """

    EQUITY = 100_000.0
    ENTRY  = 10.0   # keep position value well below 10% cap (6.7% at 666 shares)
    ATR    = 1.0   # → stop_dist=1.5 (non-catalyst)

    def _size(self, **kw) -> PositionSpec | None:
        engine = _make_engine(**{k: v for k, v in kw.items()
                                 if k not in ("ticker", "direction", "entry_price",
                                               "atr_20", "account_equity", "catalyst_flag",
                                               "regime", "cb_risk_multiplier")})
        return engine.size_position(
            ticker=kw.get("ticker", "AAPL"),
            direction=kw.get("direction", "long"),
            entry_price=kw.get("entry_price", self.ENTRY),
            atr_20=kw.get("atr_20", self.ATR),
            account_equity=kw.get("account_equity", self.EQUITY),
            catalyst_flag=kw.get("catalyst_flag", False),
            regime=kw.get("regime", RegimeLevel.NORMAL),
            cb_risk_multiplier=kw.get("cb_risk_multiplier", 1.0),
        )

    def test_basic_long_sizing(self):
        engine = _make_engine()
        spec = engine.size_position(
            ticker="AAPL", direction="long",
            entry_price=self.ENTRY, atr_20=self.ATR,
            account_equity=self.EQUITY, catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
        )
        assert spec is not None
        assert spec.shares == math.floor(1000 / 1.5)  # 666
        assert spec.ticker == "AAPL"
        assert spec.direction == "long"

    def test_stop_price_long(self):
        engine = _make_engine()
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=50.0, atr_20=1.0,
            account_equity=self.EQUITY, catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
        )
        # stop = entry - 1.5*ATR = 50 - 1.5 = 48.5
        assert spec is not None
        assert abs(spec.stop_price - 48.5) < 1e-9

    def test_stop_price_short(self):
        engine = _make_engine()
        spec = engine.size_position(
            ticker="T", direction="short",
            entry_price=50.0, atr_20=1.0,
            account_equity=self.EQUITY, catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
        )
        # stop = entry + 1.5*ATR = 50 + 1.5 = 51.5
        assert spec is not None
        assert abs(spec.stop_price - 51.5) < 1e-9

    def test_target_price_long(self):
        engine = _make_engine(tp1_r_multiple=2.0)
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=50.0, atr_20=1.0,
            account_equity=self.EQUITY, catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
        )
        # tp1 = entry + 2.0 * 1.5 = 53.0
        assert spec is not None
        assert abs(spec.target_price_1 - 53.0) < 1e-9

    def test_target_price_short(self):
        engine = _make_engine(tp1_r_multiple=2.0)
        spec = engine.size_position(
            ticker="T", direction="short",
            entry_price=50.0, atr_20=1.0,
            account_equity=self.EQUITY, catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
        )
        # tp1 = entry - 2.0 * 1.5 = 47.0
        assert spec is not None
        assert abs(spec.target_price_1 - 47.0) < 1e-9

    def test_catalyst_widens_stop(self):
        # entry=self.ENTRY (10) keeps position value below 10% cap
        engine = _make_engine()
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=self.ENTRY, atr_20=1.0,
            account_equity=self.EQUITY, catalyst_flag=True,
            regime=RegimeLevel.NORMAL,
        )
        # stop_dist = 2.25*1.0 = 2.25; shares = floor(1000/2.25) = 444; value=4440<10k ✓
        assert spec is not None
        assert spec.atr_multiplier == 2.25
        assert spec.shares == math.floor(1000 / 2.25)

    def test_elevated_regime_halves_risk(self):
        engine = _make_engine()
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=self.ENTRY, atr_20=1.0,
            account_equity=self.EQUITY, catalyst_flag=False,
            regime=RegimeLevel.ELEVATED,
        )
        # risk = 1%*100k*0.5 = 500; shares = floor(500/1.5) = 333; value=3330<10k ✓
        assert spec is not None
        assert spec.shares == math.floor(500 / 1.5)

    def test_crisis_regime_returns_none(self):
        engine = _make_engine()
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=self.ENTRY, atr_20=1.0,
            account_equity=self.EQUITY, catalyst_flag=False,
            regime=RegimeLevel.CRISIS,
        )
        assert spec is None

    def test_cb_multiplier_halves_risk(self):
        engine = _make_engine()
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=self.ENTRY, atr_20=1.0,
            account_equity=self.EQUITY, catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
            cb_risk_multiplier=0.5,
        )
        # risk = 1%*100k*1.0*0.5 = 500; shares = floor(500/1.5) = 333; value=3330<10k ✓
        assert spec is not None
        assert spec.shares == math.floor(500 / 1.5)

    def test_max_position_cap(self):
        """If ATR is tiny, shares should be capped at max_position_pct × equity / price."""
        # price=100, equity=100k, max_pct=10% → max_shares=100
        engine = _make_engine(max_position_pct=0.10, phase1_risk=0.50)  # 50% risk → huge shares
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=100.0, atr_20=0.01,
            account_equity=100_000.0, catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
        )
        assert spec is not None
        assert spec.shares <= 100   # capped at 10% of 100k / 100

    def test_zero_atr_returns_none(self):
        engine = _make_engine()
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=50.0, atr_20=0.0,
            account_equity=self.EQUITY, catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
        )
        assert spec is None

    def test_zero_equity_returns_none(self):
        engine = _make_engine()
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=50.0, atr_20=1.0,
            account_equity=0.0, catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
        )
        assert spec is None

    def test_zero_entry_price_returns_none(self):
        engine = _make_engine()
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=0.0, atr_20=1.0,
            account_equity=self.EQUITY, catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
        )
        assert spec is None

    def test_very_high_atr_yields_zero_shares(self):
        """ATR so large that floor(risk/stop_dist) < 1 → None."""
        engine = _make_engine(phase1_risk=0.001)  # 0.1% → $100 risk
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=50.0, atr_20=200.0,  # stop_dist=300 >> $100 risk
            account_equity=100_000.0, catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
        )
        assert spec is None

    def test_dollar_risk_is_shares_times_stop_distance(self):
        engine = _make_engine()
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=50.0, atr_20=1.0,
            account_equity=self.EQUITY, catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
        )
        assert spec is not None
        expected_risk = spec.shares * 1.5  # shares × stop_dist
        assert abs(spec.dollar_risk - expected_risk) < 0.01

    def test_spec_carries_regime_and_catalyst_flag(self):
        engine = _make_engine()
        spec = engine.size_position(
            ticker="T", direction="long",
            entry_price=50.0, atr_20=1.0,
            account_equity=self.EQUITY, catalyst_flag=True,
            regime=RegimeLevel.ELEVATED,
        )
        assert spec is not None
        assert spec.regime == RegimeLevel.ELEVATED
        assert spec.catalyst_flag is True


# ---------------------------------------------------------------------------
# RiskEngine.approve_new_position
# ---------------------------------------------------------------------------

class TestApproveNewPosition:

    EQUITY = 100_000.0

    def _approve(self, engine=None, **kw):
        e = engine or _make_engine()
        return e.approve_new_position(
            ticker=kw.get("ticker", "AAPL"),
            direction=kw.get("direction", "long"),
            entry_price=kw.get("entry_price", 10.0),  # low price keeps position value below 10% cap
            atr_20=kw.get("atr_20", 1.0),
            account_equity=kw.get("account_equity", self.EQUITY),
            catalyst_flag=kw.get("catalyst_flag", False),
            regime=kw.get("regime", RegimeLevel.NORMAL),
            existing_positions=kw.get("existing_positions", []),
            sector_gics=kw.get("sector_gics", None),
            sub_industry=kw.get("sub_industry", None),
            returns_60d=kw.get("returns_60d", None),
            today=kw.get("today", date(2025, 1, 15)),
        )

    def test_clean_approval(self):
        approved, spec, reason = self._approve()
        assert approved is True
        assert spec is not None
        assert reason == "ok"

    def test_approved_spec_is_position_spec(self):
        approved, spec, _ = self._approve()
        assert isinstance(spec, PositionSpec)

    def test_crisis_regime_blocks(self):
        approved, spec, reason = self._approve(regime=RegimeLevel.CRISIS)
        assert approved is False
        assert spec is None
        assert "crisis" in reason

    def test_cb3_halt_blocks(self):
        cb = CircuitBreakerManager()
        cb._state.cb3_halted = True
        engine = _make_engine(cb_manager=cb)
        approved, spec, reason = self._approve(engine=engine)
        assert approved is False
        assert spec is None
        assert "CB3" in reason

    def test_cb4_halt_blocks_within_window(self):
        today = date(2025, 1, 15)
        cb = CircuitBreakerManager()
        cb._state.cb4_halt_until = today + timedelta(days=1)
        engine = _make_engine(cb_manager=cb)
        approved, spec, reason = self._approve(engine=engine, today=today)
        assert approved is False
        assert "CB4" in reason

    def test_cb4_halt_clears_after_window(self):
        today = date(2025, 1, 15)
        cb = CircuitBreakerManager()
        cb._state.cb4_halt_until = today - timedelta(days=1)  # already expired
        engine = _make_engine(cb_manager=cb)
        approved, spec, reason = self._approve(engine=engine, today=today)
        assert approved is True

    def test_sub_industry_limit_blocks(self):
        """Two existing positions in same sub-industry → third blocked (max=2)."""
        existing = [
            PositionContext("A", "long", 5000, sub_industry="Software"),
            PositionContext("B", "long", 5000, sub_industry="Software"),
        ]
        approved, spec, reason = self._approve(
            sub_industry="Software",
            existing_positions=existing,
        )
        assert approved is False
        assert "sub_industry" in reason

    def test_unknown_sub_industry_passes(self):
        """None sub_industry → sub-industry check is skipped."""
        existing = [
            PositionContext("A", "long", 5000, sub_industry="Software"),
            PositionContext("B", "long", 5000, sub_industry="Software"),
        ]
        # candidate has no sub_industry → check is inconclusive, not blocking
        approved, spec, reason = self._approve(
            sub_industry=None,
            existing_positions=existing,
        )
        assert approved is True

    def test_overnight_exposure_cap_blocks(self):
        """Existing positions already at 55% of equity + new one → exceeds 60%."""
        equity = 100_000.0
        # 5 existing positions worth 55k total (55%)
        existing = [
            PositionContext(f"X{i}", "long", 11_000.0) for i in range(5)
        ]
        # candidate market value = shares * entry ≈ 666 * 50 = 33300 → total > 60%
        approved, spec, reason = self._approve(
            account_equity=equity,
            existing_positions=existing,
            entry_price=50.0,
            atr_20=1.0,
        )
        assert approved is False
        assert "overnight" in reason

    def test_sector_capital_soft_limit_blocks(self):
        """Sector capital > 30% → blocked."""
        equity = 100_000.0
        # 28k already in tech (28%), candidate adds ~33k → ~61%
        existing = [
            PositionContext("MSFT", "long", 28_000.0, sector_gics="Technology"),
        ]
        approved, spec, reason = self._approve(
            account_equity=equity,
            existing_positions=existing,
            sector_gics="Technology",
            entry_price=50.0,
            atr_20=1.0,
        )
        # 28k + candidate_mv: 666*50=33300 → 61.3% > 30% → blocked
        assert approved is False
        assert "sector" in reason

    def test_sector_capital_with_none_sector_passes(self):
        """Unknown sector → sector check skipped."""
        equity = 100_000.0
        existing = [
            PositionContext("MSFT", "long", 28_000.0, sector_gics="Technology"),
        ]
        approved, spec, reason = self._approve(
            account_equity=equity,
            existing_positions=existing,
            sector_gics=None,  # unknown — cannot enforce
        )
        assert approved is True

    def test_cb1_reduced_risk_flows_through(self):
        """After CB1 trigger, shares should be ~half of normal."""
        cb = CircuitBreakerManager()
        # Trigger CB1: 5 consecutive losses
        for _ in range(5):
            cb.record_trade(pnl=-100.0)
        assert cb.get_risk_multiplier() == 0.5

        engine = _make_engine(cb_manager=cb)
        approved, spec, reason = self._approve(engine=engine)
        assert approved is True
        assert spec is not None
        # Normal: floor(1000/1.5)=666; with 0.5 mult: floor(500/1.5)=333
        assert spec.shares == math.floor(500 / 1.5)

    def test_max_positions_limit_blocks(self):
        """5 existing positions → cannot add 6th (max=5)."""
        existing = [
            PositionContext(f"X{i}", "long", 1_000.0) for i in range(5)
        ]
        approved, spec, reason = self._approve(existing_positions=existing)
        assert approved is False
        assert "max_positions" in reason

    def test_returns_ticker_in_spec(self):
        approved, spec, _ = self._approve(ticker="ZZZZ")
        assert approved is True
        assert spec.ticker == "ZZZZ"

    def test_zero_atr_blocks_on_sizing(self):
        approved, spec, reason = self._approve(atr_20=0.0)
        assert approved is False
        assert spec is None

    def test_elevated_regime_produces_smaller_spec(self):
        approved_n, spec_n, _ = self._approve(regime=RegimeLevel.NORMAL)
        approved_e, spec_e, _ = self._approve(regime=RegimeLevel.ELEVATED)
        assert approved_n and approved_e
        assert spec_e.shares < spec_n.shares   # elevated → smaller position


# ---------------------------------------------------------------------------
# RegimeLevel enum values
# ---------------------------------------------------------------------------

class TestRegimeLevelEnum:

    def test_enum_values(self):
        assert RegimeLevel.NORMAL.value == "normal"
        assert RegimeLevel.ELEVATED.value == "elevated"
        assert RegimeLevel.CRISIS.value == "crisis"

    def test_three_members(self):
        assert len(RegimeLevel) == 3


# ---------------------------------------------------------------------------
# PositionSpec dataclass
# ---------------------------------------------------------------------------

class TestPositionSpec:

    def test_construction(self):
        spec = PositionSpec(
            ticker="AAPL",
            direction="long",
            entry_price=50.0,
            stop_price=48.5,
            target_price_1=53.0,
            shares=666,
            dollar_risk=999.0,
            atr_20=1.0,
            atr_multiplier=1.5,
            regime=RegimeLevel.NORMAL,
            catalyst_flag=False,
        )
        assert spec.ticker == "AAPL"
        assert spec.shares == 666
        assert spec.regime == RegimeLevel.NORMAL


# ---------------------------------------------------------------------------
# Correlation cluster integration (end-to-end through approve)
# ---------------------------------------------------------------------------

class TestCorrelationClusterViaApprove:

    def _make_returns(self, n=60, seed=0) -> np.ndarray:
        rng = np.random.default_rng(seed)
        return rng.normal(0, 0.015, n)

    def test_highly_correlated_cluster_blocks(self):
        """3 existing positions highly correlated with candidate → 4th blocked."""
        base = self._make_returns(seed=0)
        # Existing positions: returns almost identical to candidate
        existing = [
            PositionContext(
                f"X{i}", "long", 5000.0,
                returns_60d=base + np.random.default_rng(i + 10).normal(0, 0.001, 60)
            )
            for i in range(3)
        ]
        engine = _make_engine(max_correlated_cluster=3, correlation_threshold=0.6)
        approved, spec, reason = engine.approve_new_position(
            ticker="CAND", direction="long",
            entry_price=50.0, atr_20=1.0,
            account_equity=100_000.0,
            catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
            existing_positions=existing,
            returns_60d=base,
            today=date(2025, 1, 15),
        )
        assert approved is False
        assert "correlation" in reason

    def test_uncorrelated_candidate_passes(self):
        """Candidate uncorrelated with existing → passes cluster check."""
        rng = np.random.default_rng(99)
        existing = [
            PositionContext(
                f"X{i}", "long", 5000.0,
                returns_60d=np.random.default_rng(i).normal(0, 0.015, 60)
            )
            for i in range(3)
        ]
        candidate_returns = rng.normal(0, 0.015, 60)
        engine = _make_engine(max_correlated_cluster=3, correlation_threshold=0.6)
        approved, spec, reason = engine.approve_new_position(
            ticker="CAND", direction="long",
            entry_price=50.0, atr_20=1.0,
            account_equity=100_000.0,
            catalyst_flag=False,
            regime=RegimeLevel.NORMAL,
            existing_positions=existing,
            returns_60d=candidate_returns,
            today=date(2025, 1, 15),
        )
        assert approved is True
