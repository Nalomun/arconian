"""Tests for risk/cvar.py — CVaR and the four stress scenarios (F-17)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest

from risk.cvar import (
    PortfolioPosition,
    compute_cvar,
    evaluate_portfolio_risk,
    _correlation_shock_loss,
    _sector_contagion_loss,
    _liquidity_shock_loss,
    _catalyst_compound_loss,
)


def _pos(direction="long", market_value=10_000.0, atr_20=0.5, shares=0.0,
         sector="Tech", catalyst=False, returns=None):
    return PortfolioPosition(
        ticker="X",
        direction=direction,
        market_value=market_value,
        atr_20=atr_20,
        sector_gics=sector,
        catalyst_flag=catalyst,
        daily_returns=np.array(returns if returns is not None else []),
        shares=shares,
    )


class TestCompputeCvar:
    def test_insufficient_data_returns_none(self):
        assert compute_cvar(np.array([1.0, -2.0])) is None

    def test_tail_mean(self):
        pnl = np.array([-100.0, -50.0, -10.0, 0.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0,
                        5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
        cvar = compute_cvar(pnl, confidence=0.95)
        assert cvar is not None
        assert cvar <= -100.0  # the 5% tail is the worst observation(s)


class TestCorrelationShockShorts:
    def test_short_contributes_a_loss(self):
        """F-17: a short must be stressed by its biggest UP day, not clamped to $0."""
        up_returns = [0.01, -0.02, 0.12, -0.01]  # worst day for a short is +12%
        short = _pos(direction="short", market_value=10_000.0, returns=up_returns)
        loss = _correlation_shock_loss([short])
        assert loss == pytest.approx(-1200.0)  # 10_000 × -0.12

    def test_long_uses_worst_down_day(self):
        down_returns = [0.01, -0.09, 0.02, -0.03]
        long = _pos(direction="long", market_value=10_000.0, returns=down_returns)
        loss = _correlation_shock_loss([long])
        assert loss == pytest.approx(-900.0)  # 10_000 × -0.09

    def test_short_no_history_uses_default(self):
        short = _pos(direction="short", market_value=10_000.0, returns=[])
        loss = _correlation_shock_loss([short])
        assert loss == pytest.approx(-1000.0)  # 10_000 × -0.10 default


class TestSectorContagionShorts:
    def test_short_in_stressed_sector_loses(self):
        """F-17: a short in a contagion sector is hit by the worst UP day."""
        hist = {"Tech": np.array([0.01, -0.02, 0.20, -0.05])}
        short = _pos(direction="short", market_value=5_000.0, sector="Tech")
        loss = _sector_contagion_loss([short], sector_history=hist)
        assert loss == pytest.approx(-1000.0)  # 5_000 × -0.20

    def test_long_default_drawdown(self):
        long = _pos(direction="long", market_value=5_000.0, sector="Tech")
        loss = _sector_contagion_loss([long], sector_history=None)
        assert loss == pytest.approx(-750.0)  # 5_000 × -0.15 default


class TestLiquidityShockScalesWithSize:
    def test_exact_per_share_when_shares_known(self):
        pos = _pos(shares=1000, atr_20=0.5, market_value=10_000.0)
        loss = _liquidity_shock_loss([pos])
        assert loss == pytest.approx(-1000.0)  # -(2 × 0.5 × 1000)

    def test_loss_scales_with_position_size(self):
        """F-17: the old code's loss was ~constant regardless of size."""
        small = _pos(shares=100, atr_20=0.5, market_value=10_000.0)
        large = _pos(shares=1000, atr_20=0.5, market_value=100_000.0)
        loss_small = _liquidity_shock_loss([small])
        loss_large = _liquidity_shock_loss([large])
        assert loss_large == pytest.approx(10 * loss_small)

    def test_bounded_at_market_value(self):
        # 2×ATR×shares = 2×50×100 = 10_000 but market value is only 1_000.
        pos = _pos(shares=100, atr_20=50.0, market_value=1_000.0)
        loss = _liquidity_shock_loss([pos])
        assert loss == pytest.approx(-1_000.0)

    def test_fallback_fraction_when_no_shares(self):
        pos = _pos(shares=0.0, atr_20=0.5, market_value=10_000.0)
        loss = _liquidity_shock_loss([pos])
        assert loss == pytest.approx(-800.0)  # 8% fallback × 10_000


class TestCatalystCompound:
    def test_only_catalyst_positions_stressed(self):
        plain = _pos(shares=1000, atr_20=0.5, catalyst=False, market_value=50_000.0)
        cat = _pos(shares=1000, atr_20=0.5, catalyst=True, market_value=50_000.0)
        assert _catalyst_compound_loss([plain]) == 0.0
        assert _catalyst_compound_loss([cat]) == pytest.approx(-1500.0)  # 3×0.5×1000


class TestEvaluatePortfolioRisk:
    def test_empty_portfolio_passes(self):
        pnl = np.zeros(30)
        res = evaluate_portfolio_risk([], pnl, account_equity=100_000.0)
        assert res.passes_15pct_threshold is True
        assert res.worst_stress_loss == 0.0

    def test_short_position_now_drives_a_stress_loss(self):
        """End-to-end: a short portfolio produces a non-zero worst stress
        (was 0 before the F-17 sign fix)."""
        pnl = np.linspace(-200, 200, 40)
        short = _pos(direction="short", market_value=20_000.0, shares=2000,
                     atr_20=0.5, returns=[0.01, 0.15, -0.02])
        res = evaluate_portfolio_risk([short], pnl, account_equity=100_000.0)
        assert res.stress_correlation < 0
        assert res.worst_stress_loss < 0
