"""
CVaR computation and stress scenario evaluation.

Computes Conditional Value at Risk (Expected Shortfall) for the portfolio:
  95% CVaR = mean of all daily P&L observations below the 5th percentile
  99% CVaR = mean of all daily P&L observations below the 1st percentile

CVaR is subadditive and captures tail magnitude, unlike VaR which only
captures the threshold value. For fat-tailed small-cap return distributions
this difference is material.

Four stress scenarios (run monthly per Section 4.4):
  1. Correlation shock   — all positions suffer their historical worst day
  2. Liquidity shock     — all positions gapped 2×ATR against direction
  3. Sector contagion    — worst 252d sector drawdown applied to sector positions
  4. Catalyst compounding— catalyst-flagged positions gapped 3×ATR

If any scenario produces a drawdown > 15% of account equity, position count
should be reduced until the scenario passes.

Whitepaper reference: Section 4.4 (CVaR and Stress Testing)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_CVAR_95_TAIL = 0.05   # bottom 5% for 95% CVaR
_CVAR_99_TAIL = 0.01   # bottom 1% for 99% CVaR
_STRESS_MAX_DRAWDOWN = 0.15   # 15% account equity stress threshold


# ---------------------------------------------------------------------------
# Position dataclass
# ---------------------------------------------------------------------------

@dataclass
class PortfolioPosition:
    """
    Represents a single open position for CVaR and stress computation.

    Args:
        ticker: Stock symbol.
        direction: 'long' or 'short'.
        market_value: Current dollar value of the position (positive).
        atr_20: 20-day average true range in dollars.
        sector_gics: GICS sector name (e.g., 'Information Technology'). None if unknown.
        catalyst_flag: True if position has an active near-term catalyst.
        daily_returns: Array of historical daily returns for this position,
            in chronological order. Should cover 252 trading days where available.
            Used for correlation shock scenario.
        shares: Share count for the position. Used by the liquidity/catalyst gap
            scenarios to compute an exact per-share×shares dollar loss; if 0 the
            scenarios fall back to a size-proportional fraction of market_value.
    """
    ticker: str
    direction: str             # 'long' | 'short'
    market_value: float
    atr_20: float
    sector_gics: Optional[str] = None
    catalyst_flag: bool = False
    daily_returns: np.ndarray = field(default_factory=lambda: np.array([]))
    shares: float = 0.0


@dataclass
class CVaRResult:
    """Computed CVaR values and stress scenario losses."""
    cvar_95: Optional[float]         # 95% CVaR as fraction of equity (negative = loss)
    cvar_99: Optional[float]         # 99% CVaR as fraction of equity
    stress_correlation: float        # correlation shock loss (dollars)
    stress_liquidity: float          # liquidity shock loss (dollars)
    stress_sector: float             # sector contagion loss (dollars)
    stress_catalyst: float           # catalyst compounding loss (dollars)
    worst_stress_loss: float         # max of the four scenarios
    passes_15pct_threshold: bool     # True if all stresses < 15% of equity
    n_observations: int              # daily P&L obs used for CVaR


# ---------------------------------------------------------------------------
# CVaR computation
# ---------------------------------------------------------------------------

def compute_cvar(
    daily_pnl: np.ndarray,
    confidence: float = 0.95,
) -> Optional[float]:
    """
    Compute Conditional Value at Risk (Expected Shortfall).

    Args:
        daily_pnl: Array of daily portfolio P&L observations in dollars
            (negative = loss). Should contain at least 20 observations.
        confidence: Confidence level (0.95 for 95% CVaR, 0.99 for 99%).

    Returns:
        CVaR as a dollar value (negative = expected loss in the tail).
        Returns None if fewer than 10 observations available.
    """
    pnl = np.asarray(daily_pnl, dtype=float)
    n = len(pnl)

    if n < 10:
        logger.debug("compute_cvar: insufficient data (%d obs, need ≥ 10)", n)
        return None

    tail_threshold = np.quantile(pnl, 1 - confidence)
    tail_obs = pnl[pnl <= tail_threshold]

    if len(tail_obs) == 0:
        return float(tail_threshold)

    return float(np.mean(tail_obs))


# ---------------------------------------------------------------------------
# Stress scenarios
# ---------------------------------------------------------------------------

def _gap_loss(
    pos: PortfolioPosition,
    atr_multiple: float,
    fallback_fraction: float,
) -> float:
    """
    Dollar loss (positive) from an ``atr_multiple``×ATR adverse gap on ``pos``.

    Exact when shares are known: gap_per_share (atr_multiple × atr_20) × shares,
    so the loss scales with position size. When shares are unavailable, falls
    back to a size-proportional ``fallback_fraction`` of market value. Either
    way the loss scales with size — the old code divided ATR by market value,
    which cancelled size and produced a roughly constant loss per position
    regardless of how large it was (F-17). Bounded at the position's market
    value so the simple model can't report losing more than the position holds.
    """
    if pos.shares > 0:
        loss = atr_multiple * pos.atr_20 * pos.shares
    else:
        loss = fallback_fraction * pos.market_value
    return min(loss, pos.market_value)


def _correlation_shock_loss(positions: list[PortfolioPosition]) -> float:
    """
    Scenario 1: All positions suffer their worst single-day return simultaneously.
    Simulates a correlation-1 event (broad market panic).

    Returns: Total portfolio loss in dollars (negative value).
    """
    total_loss = 0.0
    for pos in positions:
        history = pos.daily_returns[-252:] if len(pos.daily_returns) else None

        # Each position is hit with ITS OWN worst-case day for its direction:
        # a long suffers on the biggest DOWN day (min return); a short suffers on
        # the biggest UP day (max return). The old code used min() for both and
        # then took -min() for shorts — a positive number — which min(.,0) clamped
        # to $0, so shorts contributed nothing to the shock (F-17).
        if pos.direction == "long":
            worst_return = float(np.min(history)) if history is not None else -0.10
            pos_pnl = pos.market_value * worst_return
        else:  # short
            worst_up = float(np.max(history)) if history is not None else 0.10
            pos_pnl = pos.market_value * (-worst_up)

        total_loss += min(pos_pnl, 0.0)  # only count losses

    return total_loss


def _liquidity_shock_loss(positions: list[PortfolioPosition]) -> float:
    """
    Scenario 2: All positions gap 2×ATR against the trade direction at open.
    Simulates an overnight gap that passes through the stop.

    Returns: Total portfolio loss in dollars (negative value).
    """
    total_loss = 0.0
    for pos in positions:
        total_loss -= _gap_loss(pos, atr_multiple=2.0, fallback_fraction=0.08)
    return total_loss


def _sector_contagion_loss(
    positions: list[PortfolioPosition],
    sector_history: Optional[dict[str, np.ndarray]] = None,
) -> float:
    """
    Scenario 3: Apply the worst sector drawdown in the trailing 252 days
    to all positions in that sector simultaneously.

    Args:
        positions: Current open positions.
        sector_history: Dict mapping sector name → 252d daily return array.
            If None, uses a conservative -15% worst-sector-day assumption.

    Returns: Total portfolio loss in dollars (negative value).
    """
    # Group positions by sector
    sector_positions: dict[str, list[PortfolioPosition]] = {}
    for pos in positions:
        sector = pos.sector_gics or "_unknown"
        sector_positions.setdefault(sector, []).append(pos)

    total_loss = 0.0
    for sector, sector_pos in sector_positions.items():
        # Worst-case sector move per direction: longs are hit by the worst
        # sector DOWN day (min), shorts by the worst sector UP day (max). The
        # old code used min() for both and negated it for shorts → a positive
        # number that min(.,0) clamped to $0, so shorts in a stressed sector
        # contributed nothing (F-17).
        if sector_history and sector in sector_history:
            returns = np.asarray(sector_history[sector])[-252:]
            worst_down = float(np.min(returns))
            worst_up = float(np.max(returns))
        else:
            worst_down = -0.15   # conservative default
            worst_up = 0.15

        for pos in sector_pos:
            if pos.direction == "long":
                loss = pos.market_value * worst_down
            else:
                loss = pos.market_value * (-worst_up)
            total_loss += min(loss, 0.0)

    return total_loss


def _catalyst_compound_loss(positions: list[PortfolioPosition]) -> float:
    """
    Scenario 4: All catalyst-flagged positions gap 3×ATR against direction.
    Only catalyst positions are stressed in this scenario.

    Returns: Total portfolio loss in dollars (negative value).
    """
    total_loss = 0.0
    for pos in positions:
        if not pos.catalyst_flag:
            continue
        # 3×ATR gap, same per-share×shares model as the liquidity shock.
        total_loss -= _gap_loss(pos, atr_multiple=3.0, fallback_fraction=0.12)

    return total_loss


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def evaluate_portfolio_risk(
    positions: list[PortfolioPosition],
    daily_pnl_history: np.ndarray,
    account_equity: float,
    sector_history: Optional[dict[str, np.ndarray]] = None,
) -> CVaRResult:
    """
    Compute CVaR and run all four stress scenarios for the current portfolio.

    Args:
        positions: List of currently open positions.
        daily_pnl_history: Trailing 252-day portfolio daily P&L in dollars.
        account_equity: Current account equity in dollars.
        sector_history: Optional dict of sector name → daily return history.
            Used for the sector contagion stress scenario.

    Returns:
        CVaRResult with all computed metrics.
    """
    pnl = np.asarray(daily_pnl_history, dtype=float)

    cvar_95 = compute_cvar(pnl, confidence=0.95)
    cvar_99 = compute_cvar(pnl, confidence=0.99)

    # Express CVaR as fraction of equity for comparability
    cvar_95_frac = (cvar_95 / account_equity) if (cvar_95 is not None and account_equity > 0) else None
    cvar_99_frac = (cvar_99 / account_equity) if (cvar_99 is not None and account_equity > 0) else None

    if not positions:
        return CVaRResult(
            cvar_95=cvar_95_frac,
            cvar_99=cvar_99_frac,
            stress_correlation=0.0,
            stress_liquidity=0.0,
            stress_sector=0.0,
            stress_catalyst=0.0,
            worst_stress_loss=0.0,
            passes_15pct_threshold=True,
            n_observations=len(pnl),
        )

    s1 = _correlation_shock_loss(positions)
    s2 = _liquidity_shock_loss(positions)
    s3 = _sector_contagion_loss(positions, sector_history)
    s4 = _catalyst_compound_loss(positions)
    worst = min(s1, s2, s3, s4)  # all are negative → min = largest loss

    threshold = -account_equity * _STRESS_MAX_DRAWDOWN if account_equity > 0 else 0.0
    passes = (worst >= threshold)

    if not passes:
        logger.warning(
            "Stress test FAILED: worst_loss=%.1f%% of equity (threshold=%.0f%%)",
            100 * worst / account_equity if account_equity > 0 else 0,
            100 * _STRESS_MAX_DRAWDOWN,
        )

    logger.debug(
        "CVaR: 95%%=%.1f%% 99%%=%.1f%% | Stress: corr=%.0f liq=%.0f sect=%.0f cat=%.0f",
        100 * (cvar_95_frac or 0),
        100 * (cvar_99_frac or 0),
        s1, s2, s3, s4,
    )

    return CVaRResult(
        cvar_95=cvar_95_frac,
        cvar_99=cvar_99_frac,
        stress_correlation=s1,
        stress_liquidity=s2,
        stress_sector=s3,
        stress_catalyst=s4,
        worst_stress_loss=worst,
        passes_15pct_threshold=passes,
        n_observations=len(pnl),
    )
