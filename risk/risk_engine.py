"""
Risk Engine — regime detection, position sizing, and full approval gate.

Orchestrates the four risk subsystems:
  1. Regime detection (RegimeLevel): classifies market as Normal/Elevated/Crisis
     using VIX level and IWM rolling returns.
  2. Position sizing (PositionSpec): ATR-based stop distance with catalyst premium,
     capped at max_position_pct of equity.
  3. Circuit breaker gate: checks CB3 halt and CB4 pause before sizing.
  4. Portfolio constraint gate: runs all four PortfolioConstraintChecker checks.

The single entry point for the rest of the system is approve_new_position(),
which returns (approved, spec_or_None, reason_string).

Whitepaper reference: Sections 4.1–4.5
"""

import logging
import math
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional

from config.config_loader import ConfigLoader
from risk.circuit_breakers import CircuitBreakerManager
from risk.portfolio_constraints import PortfolioConstraintChecker, PositionContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regime
# ---------------------------------------------------------------------------

class RegimeLevel(Enum):
    """
    Market regime classification.

    NORMAL   — VIX ≤ 25 AND IWM 10d return ≥ -5%
    ELEVATED — VIX > 25 OR IWM 10d return < -5%
    CRISIS   — VIX > 35 OR IWM 20d return < -10%

    Crisis takes precedence: checked first. Elevated checked second.
    """
    NORMAL = "normal"
    ELEVATED = "elevated"
    CRISIS = "crisis"


def check_regime(
    vix: float,
    iwm_10d_return: float,
    iwm_20d_return: float,
    vix_elevated_threshold: float = 25.0,
    vix_crisis_threshold: float = 35.0,
    iwm_elevated_10d: float = -0.05,
    iwm_crisis_20d: float = -0.10,
) -> RegimeLevel:
    """
    Classify current market regime from VIX and IWM rolling returns.

    Crisis is checked before Elevated; a VIX of 40 with IWM -12% is CRISIS,
    not ELEVATED. Order matters.

    Args:
        vix: Current VIX index level.
        iwm_10d_return: IWM trailing 10-day return (e.g. -0.07 for -7%).
        iwm_20d_return: IWM trailing 20-day return.
        vix_elevated_threshold: VIX level that triggers Elevated (default 25).
        vix_crisis_threshold: VIX level that triggers Crisis (default 35).
        iwm_elevated_10d: IWM 10d return below which Elevated is triggered.
        iwm_crisis_20d: IWM 20d return below which Crisis is triggered.

    Returns:
        RegimeLevel enum value.
    """
    if vix > vix_crisis_threshold or iwm_20d_return < iwm_crisis_20d:
        return RegimeLevel.CRISIS
    if vix > vix_elevated_threshold or iwm_10d_return < iwm_elevated_10d:
        return RegimeLevel.ELEVATED
    return RegimeLevel.NORMAL


# ---------------------------------------------------------------------------
# Position spec
# ---------------------------------------------------------------------------

@dataclass
class PositionSpec:
    """
    Fully-sized position specification approved for entry.

    Produced by RiskEngine.size_position() and included in the result of
    approve_new_position() when all checks pass.
    """
    ticker: str
    direction: str              # 'long' | 'short'
    entry_price: float
    stop_price: float
    target_price_1: float       # TP1 at tp1_r_multiple × R
    shares: int
    dollar_risk: float          # shares × stop_distance
    atr_20: float
    atr_multiplier: float       # 1.5 normal | 2.25 catalyst
    regime: RegimeLevel
    catalyst_flag: bool


# ---------------------------------------------------------------------------
# Risk Engine
# ---------------------------------------------------------------------------

class RiskEngine:
    """
    Orchestrates all risk checks and position sizing for each candidate signal.

    Args:
        config: Loaded ConfigLoader instance.
        cb_manager: CircuitBreakerManager for this account.
        constraint_checker: PortfolioConstraintChecker (uses config defaults
            if not supplied; supplied explicitly in tests).
    """

    # Regime multipliers: fraction of base risk to deploy per regime
    _REGIME_MULT = {
        RegimeLevel.NORMAL:   1.0,
        RegimeLevel.ELEVATED: 0.5,
        RegimeLevel.CRISIS:   0.0,   # no new positions in crisis
    }

    def __init__(
        self,
        config: ConfigLoader,
        cb_manager: CircuitBreakerManager,
        constraint_checker: Optional[PortfolioConstraintChecker] = None,
    ) -> None:
        self._cfg = config
        self._cb = cb_manager
        self._constraints = constraint_checker or PortfolioConstraintChecker(
            max_positions=config.risk.max_concurrent_positions,
            max_sub_industry=2,
            max_sector_capital_pct=config.risk.max_sector_capital_pct,
            max_correlated_cluster=config.risk.max_correlated_cluster,
            correlation_threshold=config.risk.correlation_threshold,
            max_overnight_exposure_pct=config.risk.max_overnight_exposure_pct,
        )

    # ------------------------------------------------------------------
    # Regime
    # ------------------------------------------------------------------

    def current_regime(
        self,
        vix: float,
        iwm_10d_return: float,
        iwm_20d_return: float,
    ) -> RegimeLevel:
        """
        Classify current regime using config thresholds.

        Args:
            vix: Current VIX level.
            iwm_10d_return: IWM trailing 10-day return.
            iwm_20d_return: IWM trailing 20-day return.
        """
        r = self._cfg.risk
        return check_regime(
            vix=vix,
            iwm_10d_return=iwm_10d_return,
            iwm_20d_return=iwm_20d_return,
            vix_elevated_threshold=r.vix_elevated_threshold,
            vix_crisis_threshold=r.vix_crisis_threshold,
            iwm_elevated_10d=r.iwm_elevated_10d_return,
            iwm_crisis_20d=r.iwm_crisis_20d_return,
        )

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    def size_position(
        self,
        ticker: str,
        direction: str,
        entry_price: float,
        atr_20: float,
        account_equity: float,
        catalyst_flag: bool,
        regime: RegimeLevel,
        cb_risk_multiplier: float = 1.0,
    ) -> Optional[PositionSpec]:
        """
        Compute share count and stop/target prices for a proposed position.

        Formula:
            risk_dollars = phase1_risk_per_trade × equity × regime_mult × cb_mult
            atr_multiplier = atr_stop_multiplier × (catalyst_atr_premium if catalyst else 1.0)
                           = 1.5 × 1.5 = 2.25 for a catalyst, 1.5 otherwise
            stop_distance = atr_multiplier × atr_20
            shares = floor(risk_dollars / stop_distance)
            capped: position_value = shares × entry_price ≤ max_position_pct × equity

        Args:
            ticker: Symbol.
            direction: 'long' or 'short'.
            entry_price: Expected fill price.
            atr_20: 20-day average true range in dollars.
            account_equity: Current account equity.
            catalyst_flag: True if a near-term catalyst is present (widens stop).
            regime: Current RegimeLevel.
            cb_risk_multiplier: Circuit-breaker multiplier (0.5 or 1.0).

        Returns:
            PositionSpec if at least 1 share survives all caps, else None.
        """
        if regime == RegimeLevel.CRISIS:
            logger.debug("size_position: regime CRISIS → no sizing")
            return None

        if atr_20 <= 0 or entry_price <= 0 or account_equity <= 0:
            logger.debug(
                "size_position: invalid inputs (entry=%.4f atr=%.4f equity=%.2f)",
                entry_price, atr_20, account_equity,
            )
            return None

        r = self._cfg.risk
        regime_mult = self._REGIME_MULT[regime]
        base_risk = r.phase1_risk_per_trade * account_equity * regime_mult * cb_risk_multiplier

        # catalyst_atr_premium is a PREMIUM factor (config 1.5), not the full
        # multiplier — it multiplies the base stop, matching order_manager's
        # authoritative sizing (1.5 × 1.5 = 2.25). The old code used it as the
        # whole multiplier, so a catalyst got the same 1.5× stop as a non-catalyst
        # — the premium was silently dropped (F-17).
        atr_mult = r.atr_stop_multiplier * (r.catalyst_atr_premium if catalyst_flag else 1.0)
        stop_distance = atr_mult * atr_20

        if stop_distance <= 0:
            return None

        raw_shares = base_risk / stop_distance
        shares = math.floor(raw_shares)

        if shares < 1:
            logger.debug(
                "size_position %s: risk=%.2f stop_dist=%.4f → 0 shares",
                ticker, base_risk, stop_distance,
            )
            return None

        # Cap at max_position_pct of equity
        max_shares_by_cap = math.floor(r.max_position_pct * account_equity / entry_price)
        shares = min(shares, max_shares_by_cap)

        if shares < 1:
            logger.debug(
                "size_position %s: capped to 0 shares (max_pct=%.1f%% equity=%.2f price=%.4f)",
                ticker, 100 * r.max_position_pct, account_equity, entry_price,
            )
            return None

        dollar_risk = shares * stop_distance

        # tp1_r_multiple lives in the EXECUTION config (where order_manager reads
        # it), not risk. Reading r.tp1_r_multiple raised AttributeError with the
        # real config — masked only because the unit-test mock set it on .risk.
        tp1_r_multiple = self._cfg.execution.tp1_r_multiple

        # Stop and target
        if direction == "long":
            stop_price = entry_price - stop_distance
            target_price_1 = entry_price + tp1_r_multiple * stop_distance
        else:
            stop_price = entry_price + stop_distance
            target_price_1 = entry_price - tp1_r_multiple * stop_distance

        logger.debug(
            "size_position %s %s: shares=%d entry=%.4f stop=%.4f tp1=%.4f "
            "risk=%.2f regime=%s catalyst=%s",
            ticker, direction, shares, entry_price, stop_price, target_price_1,
            dollar_risk, regime.value, catalyst_flag,
        )

        return PositionSpec(
            ticker=ticker,
            direction=direction,
            entry_price=entry_price,
            stop_price=round(stop_price, 4),
            target_price_1=round(target_price_1, 4),
            shares=shares,
            dollar_risk=dollar_risk,
            atr_20=atr_20,
            atr_multiplier=atr_mult,
            regime=regime,
            catalyst_flag=catalyst_flag,
        )

    # ------------------------------------------------------------------
    # Full approval gate
    # ------------------------------------------------------------------

    def approve_new_position(
        self,
        ticker: str,
        direction: str,
        entry_price: float,
        atr_20: float,
        account_equity: float,
        catalyst_flag: bool,
        regime: RegimeLevel,
        existing_positions: list[PositionContext],
        sector_gics: Optional[str] = None,
        sub_industry: Optional[str] = None,
        returns_60d=None,
        today: Optional[date] = None,
    ) -> tuple[bool, Optional[PositionSpec], str]:
        """
        Run all risk checks and return an approval decision.

        Sequence:
          1. Circuit breaker gate (CB3 halt, CB4 pause)
          2. Crisis regime check
          3. Position sizing
          4. Portfolio constraints (sub-industry, sector capital, correlation, overnight)

        Args:
            ticker: Symbol being considered.
            direction: 'long' or 'short'.
            entry_price: Expected fill price.
            atr_20: 20-day ATR in dollars.
            account_equity: Current account equity.
            catalyst_flag: True if near-term catalyst present.
            regime: Current RegimeLevel (caller computes via current_regime()).
            existing_positions: Currently open PositionContext objects.
            sector_gics: GICS sector of the candidate (for sector capital check).
            sub_industry: Sub-industry of the candidate (for sub-industry limit).
            returns_60d: 60d daily return array (for correlation cluster check).
            today: Current date (for circuit breaker time checks).

        Returns:
            (approved, spec, reason)
            - approved: True only if ALL checks pass.
            - spec: PositionSpec if approved, None otherwise.
            - reason: 'ok' or short description of first blocking check.
        """
        # 1. Circuit breaker gate
        cb_allowed, cb_reason = self._cb.allow_new_positions(today=today)
        if not cb_allowed:
            logger.debug("approve %s: blocked by circuit breaker: %s", ticker, cb_reason)
            return False, None, cb_reason

        # 2. Regime check (no new positions in crisis)
        if regime == RegimeLevel.CRISIS:
            return False, None, "regime_crisis"

        cb_mult = self._cb.get_risk_multiplier()

        # 3. Size the position
        spec = self.size_position(
            ticker=ticker,
            direction=direction,
            entry_price=entry_price,
            atr_20=atr_20,
            account_equity=account_equity,
            catalyst_flag=catalyst_flag,
            regime=regime,
            cb_risk_multiplier=cb_mult,
        )
        if spec is None:
            return False, None, "position_too_small_or_invalid"

        # 4. Portfolio constraint checks
        candidate = PositionContext(
            ticker=ticker,
            direction=direction,
            market_value=spec.shares * entry_price,
            sector_gics=sector_gics,
            sub_industry=sub_industry,
            returns_60d=returns_60d,
        )
        approved, failures = self._constraints.check_all(
            candidate=candidate,
            existing=existing_positions,
            account_equity=account_equity,
        )
        if not approved:
            reason = "; ".join(failures)
            logger.debug("approve %s: portfolio constraints failed: %s", ticker, reason)
            return False, None, reason

        logger.info(
            "approve %s %s: APPROVED — %d shares @ %.4f risk=%.2f regime=%s",
            ticker, direction, spec.shares, entry_price, spec.dollar_risk, regime.value,
        )
        return True, spec, "ok"
