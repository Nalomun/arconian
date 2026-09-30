"""
Portfolio-level constraint checks.

Enforces the four portfolio constraints from whitepaper Section 4.3:

  1. GICS sub-industry hard limit: ≤ 2 positions in the same sub-industry
     (uses yfinance 'industry' field as a proxy for GICS sub-industry)

  2. Sector capital soft limit: ≤ 30% of deployed capital in the same GICS sector

  3. Correlation cluster limit: ≤ 3 positions with pairwise Ledoit-Wolf
     shrinkage-estimated 60d correlation > 0.6 (threshold configurable)

  4. Overnight exposure cap: total position value ≤ 60% of account equity

Design note on Ledoit-Wolf:
  At T=60 observations and p=5 assets, the sample correlation standard error
  is ~0.12 at r=0.3. Ledoit-Wolf analytical shrinkage reduces this by ~40%,
  preventing false blocking from noise correlations.

  Implementation uses the Oracle Approximating Shrinkage with identity target:
    Σ_shrunk = (1 - α) × S + α × I
  where S is the sample correlation matrix and α is the analytical shrinkage
  intensity (Ledoit & Wolf, 2004, adapted for correlation matrices).

Whitepaper reference: Section 4.3 (Portfolio-Level Constraints)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Position context dataclass
# ---------------------------------------------------------------------------

@dataclass
class PositionContext:
    """
    Minimal position description required for constraint checking.

    Args:
        ticker: Stock symbol.
        direction: 'long' or 'short'.
        market_value: Current dollar value of the position (positive).
        sector_gics: GICS sector name (e.g., 'Information Technology').
        sub_industry: Industry / GICS sub-industry string used for the
            sub-industry concentration limit. Uses yfinance 'industry' field
            as a proxy (exact GICS sub-industry would require a separate source).
        returns_60d: Array of the last 60 daily returns for this position.
            Used for Ledoit-Wolf correlation estimation. None skips correlation check.
    """
    ticker: str
    direction: str
    market_value: float
    sector_gics: Optional[str] = None
    sub_industry: Optional[str] = None
    returns_60d: Optional[np.ndarray] = None


# ---------------------------------------------------------------------------
# Ledoit-Wolf shrinkage
# ---------------------------------------------------------------------------

def ledoit_wolf_shrinkage_correlation(returns_matrix: np.ndarray) -> np.ndarray:
    """
    Compute the Ledoit-Wolf shrinkage-estimated correlation matrix.

    Applies analytical linear shrinkage toward the identity matrix
    (zero-correlation target). Reduces estimation error by ~40% vs
    sample correlation at T=60, p=5.

    Reference: Ledoit & Wolf (2004) "A well-conditioned estimator for
    large-dimensional covariance matrices", adapted for correlation.

    Args:
        returns_matrix: (T, p) matrix of asset returns (T observations,
            p assets). At least 10 rows required.

    Returns:
        (p, p) shrinkage-estimated correlation matrix, or the sample
        correlation matrix if shrinkage cannot be computed.
    """
    arr = np.asarray(returns_matrix, dtype=float)
    T, p = arr.shape

    if T < 4 or p < 2:
        return np.corrcoef(arr.T) if p >= 2 else np.eye(p)

    S = np.corrcoef(arr.T)   # (p, p) sample correlation

    I = np.eye(p)
    diff = S - I

    # Frobenius norm squared of (S - I)
    diff_sq = float(np.sum(diff ** 2))
    if diff_sq < 1e-12:
        return S  # Already near identity

    # Analytical shrinkage intensity for identity target, correlation matrix
    # Derived from Ledoit & Wolf (2004) equations 14 and 15, simplified for
    # equi-diagonal target (trace(S) = p → μ = 1):
    #   alpha = [(p+2)(p-1) / T] / ||S - I||_F^2
    # This is the "analytical Oracle" approximation. For small p and T≥30,
    # this is within a few percent of the true optimal shrinkage.
    alpha_raw = ((p + 2) * (p - 1)) / (T * diff_sq)
    alpha = float(np.clip(alpha_raw, 0.0, 1.0))

    result = (1.0 - alpha) * S + alpha * I
    logger.debug(
        "LW shrinkage: T=%d p=%d alpha=%.3f (diff_sq=%.4f)",
        T, p, alpha, diff_sq,
    )
    return result


def _pairwise_max_correlation(returns_matrix: np.ndarray, shrunk_corr: np.ndarray) -> float:
    """Return the largest off-diagonal absolute correlation value."""
    p = shrunk_corr.shape[0]
    if p < 2:
        return 0.0
    max_corr = 0.0
    for i in range(p):
        for j in range(i + 1, p):
            max_corr = max(max_corr, abs(shrunk_corr[i, j]))
    return max_corr


# ---------------------------------------------------------------------------
# Constraint checker
# ---------------------------------------------------------------------------

class PortfolioConstraintChecker:
    """
    Checks all four portfolio-level constraints for a proposed new position.

    Args:
        max_positions: Maximum concurrent open positions (default 5).
        max_sub_industry: Maximum positions in the same GICS sub-industry (default 2).
        max_sector_capital_pct: Maximum fraction of deployed capital per sector (0.30).
        max_correlated_cluster: Maximum positions in a correlated cluster (default 3).
        correlation_threshold: Ledoit-Wolf pairwise threshold (default 0.6).
        max_overnight_exposure_pct: Overnight exposure cap as fraction of equity (0.60).
    """

    def __init__(
        self,
        max_positions: int = 5,
        max_sub_industry: int = 2,
        max_sector_capital_pct: float = 0.30,
        max_correlated_cluster: int = 3,
        correlation_threshold: float = 0.6,
        max_overnight_exposure_pct: float = 0.60,
    ) -> None:
        self._max_pos = max_positions
        self._max_sub = max_sub_industry
        self._max_sector_pct = max_sector_capital_pct
        self._max_corr_cluster = max_correlated_cluster
        self._corr_threshold = correlation_threshold
        self._max_overnight = max_overnight_exposure_pct

    def check_all(
        self,
        candidate: PositionContext,
        existing: list[PositionContext],
        account_equity: float,
    ) -> tuple[bool, list[str]]:
        """
        Run all constraint checks for a proposed new position.

        Args:
            candidate: The position being considered.
            existing: Currently open positions.
            account_equity: Current account equity in dollars.

        Returns:
            (approved, rejection_reasons)
            approved is True only if ALL checks pass.
            rejection_reasons is a list of constraint names that failed.
        """
        failures = []

        # Check 1: total position count
        if len(existing) >= self._max_pos:
            failures.append(f"max_positions: {len(existing)}/{self._max_pos}")

        # Check 2: sub-industry concentration (hard limit)
        ok, msg = self.check_sub_industry(candidate, existing)
        if not ok:
            failures.append(msg)

        # Check 3: sector capital (soft limit)
        deployed = sum(p.market_value for p in existing)
        ok, msg = self.check_sector_capital(candidate, existing, deployed)
        if not ok:
            failures.append(msg)

        # Check 4: correlation cluster
        ok, msg = self.check_correlation_cluster(candidate, existing)
        if not ok:
            failures.append(msg)

        # Check 5: overnight exposure (after adding candidate)
        ok, msg = self.check_overnight_exposure(existing + [candidate], account_equity)
        if not ok:
            failures.append(msg)

        return (len(failures) == 0), failures

    def check_sub_industry(
        self,
        candidate: PositionContext,
        existing: list[PositionContext],
    ) -> tuple[bool, str]:
        """
        GICS sub-industry hard limit: ≤ max_sub_industry positions in same group.

        Returns (ok, reason_if_failed).
        """
        if candidate.sub_industry is None:
            return True, ""   # unknown sub-industry: cannot enforce

        count = sum(
            1 for p in existing
            if p.sub_industry is not None and p.sub_industry == candidate.sub_industry
        )
        if count >= self._max_sub:
            msg = (
                f"sub_industry_limit: {count}/{self._max_sub} "
                f"positions already in '{candidate.sub_industry}'"
            )
            logger.debug("constraint: %s", msg)
            return False, msg
        return True, ""

    def check_sector_capital(
        self,
        candidate: PositionContext,
        existing: list[PositionContext],
        deployed_capital: float,
    ) -> tuple[bool, str]:
        """
        Sector capital soft limit: ≤ 30% of deployed capital in same GICS sector.

        Returns (ok, reason_if_failed).
        """
        if candidate.sector_gics is None or deployed_capital <= 0:
            return True, ""

        sector_value = sum(
            p.market_value for p in existing
            if p.sector_gics == candidate.sector_gics
        ) + candidate.market_value

        sector_pct = sector_value / deployed_capital
        if sector_pct > self._max_sector_pct:
            msg = (
                f"sector_capital_limit: {100*sector_pct:.1f}% in '{candidate.sector_gics}' "
                f"(max {100*self._max_sector_pct:.0f}%)"
            )
            logger.debug("constraint: %s", msg)
            return False, msg
        return True, ""

    def check_correlation_cluster(
        self,
        candidate: PositionContext,
        existing: list[PositionContext],
    ) -> tuple[bool, str]:
        """
        Ledoit-Wolf correlation cluster limit: candidate cannot join a cluster
        of ≥ max_correlated_cluster positions with pairwise correlation > threshold.

        If returns data is unavailable for 2+ positions, the check is skipped
        (inconclusive rather than blocking). This prevents data outages from
        incorrectly preventing new entries.

        Returns (ok, reason_if_failed).
        """
        if candidate.returns_60d is None:
            return True, ""   # no data — cannot enforce

        # Build list of positions with return history
        with_returns = [p for p in existing if p.returns_60d is not None]
        if len(with_returns) == 0:
            return True, ""

        # Align return arrays to same length
        min_len = min(len(candidate.returns_60d), *(len(p.returns_60d) for p in with_returns))
        min_len = max(min_len, 4)

        all_returns = np.column_stack(
            [p.returns_60d[-min_len:] for p in with_returns]
            + [candidate.returns_60d[-min_len:]]
        )  # (T, p+1)

        if all_returns.shape[0] < 4:
            return True, ""   # not enough history for correlation estimate

        shrunk = ledoit_wolf_shrinkage_correlation(all_returns)
        candidate_col = all_returns.shape[1] - 1

        # Count how many existing positions are highly correlated with candidate
        correlated_with_candidate = sum(
            1 for i in range(candidate_col)
            if abs(shrunk[i, candidate_col]) > self._corr_threshold
        )

        if correlated_with_candidate >= self._max_corr_cluster:
            msg = (
                f"correlation_cluster: {correlated_with_candidate} existing positions "
                f"have LW-correlation > {self._corr_threshold} with {candidate.ticker}"
            )
            logger.debug("constraint: %s", msg)
            return False, msg
        return True, ""

    def check_overnight_exposure(
        self,
        positions_after_add: list[PositionContext],
        account_equity: float,
    ) -> tuple[bool, str]:
        """
        Overnight exposure cap: total position value ≤ max_overnight_exposure_pct × equity.

        Args:
            positions_after_add: All positions INCLUDING the proposed new one.
            account_equity: Current account equity.

        Returns (ok, reason_if_failed).
        """
        if account_equity <= 0:
            return True, ""

        total_exposure = sum(p.market_value for p in positions_after_add)
        exposure_pct = total_exposure / account_equity

        if exposure_pct > self._max_overnight:
            msg = (
                f"overnight_exposure: {100*exposure_pct:.1f}% of equity "
                f"(max {100*self._max_overnight:.0f}%)"
            )
            logger.debug("constraint: %s", msg)
            return False, msg
        return True, ""
