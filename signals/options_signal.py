"""
Options Flow Composite signal — Pan & Poteshman (2006) framework.

Composite of three sub-components:
  1. Call/put volume ratio percentile rank (40% weight)
  2. IV rank: (current_IV − 52w_low) / (52w_high − 52w_low)  (30% weight)
  3. Volume/OI ratio percentile rank  (30% weight)

Safeguards — signal returns None if ANY of the following are violated:
  - Total daily options volume < min_daily_volume (default 200 contracts)
  - Total aggregate OI < min_oi (default 1,500)
  - Strikes with OI > 0 < min_strikes (default 4)
  - Distinct lot sizes < min_trade_sizes (default 3)

Additional adjustments:
  - IV range < iv_range_min_ppt (default 5 pp) → iv_rank set to 0.5 (neutral),
    flagged as iv_range_insufficient
  - Single-strike volume domination detected → hedging_downweighted flag,
    composite halved to reflect likely delta-hedge activity

Note on weight: 0.15 initial weight (reduced from v2's 0.25) per
Pan & Poteshman finding that publicly observable options data has less
predictive power than proprietary direction-tagged flow.

Whitepaper reference: Section 3.2.3 (Options Flow Composite)
Supplements reference: Doc 1, Section 1.1.C (options chain contract)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


def _pctile_rank(value: float, history: np.ndarray) -> float:
    """Fraction of `history` values strictly below `value`. Range [0, 1]."""
    if len(history) == 0:
        return 0.5
    return float(np.mean(history < value))


@dataclass
class OptionsSignalResult:
    """
    Full result of the options composite computation.

    `composite` is None when any of the mandatory safeguards are not met.
    Individual sub-component values are populated where computable, even when
    the composite is suppressed, so they can be logged for IC analysis.
    """

    composite: Optional[float]            # Final [0, 1] composite; None if suppressed

    # Sub-components (may be None if data unavailable)
    pc_ratio_pctile: Optional[float]      # Call/put vol ratio percentile rank
    iv_rank: Optional[float]              # IV rank [0, 1]
    vol_oi_pctile: Optional[float]        # Volume/OI percentile rank

    # Suppression state
    suppressed_reason: Optional[str]      # Why composite is None, or None if computed

    # Diagnostic flags
    iv_range_insufficient: bool = False   # True if 52w IV range < min threshold
    hedging_downweighted: bool = False    # True if single-strike domination detected


def compute_options_signal(
    total_call_vol: float,
    total_put_vol: float,
    total_oi: float,
    strike_count_with_oi: int,
    distinct_lot_sizes: int,
    current_iv: Optional[float],
    iv_52w_high: Optional[float] = None,
    iv_52w_low: Optional[float] = None,
    history_pc_ratios: Optional[np.ndarray] = None,
    history_vol_oi: Optional[np.ndarray] = None,
    dominant_strike_vol_pct: Optional[float] = None,
    min_daily_volume: int = 200,
    min_oi: int = 1500,
    min_strikes: int = 4,
    min_trade_sizes: int = 3,
    iv_range_min_ppt: float = 5.0,
) -> OptionsSignalResult:
    """
    Compute the options flow composite signal with all safeguards applied.

    Args:
        total_call_vol: Total call option contracts traded today.
        total_put_vol: Total put option contracts traded today.
        total_oi: Aggregate open interest across all near-term expirations.
        strike_count_with_oi: Number of distinct strikes with OI > 0.
        distinct_lot_sizes: Number of distinct trade sizes observed today
            (rounded to nearest 10 contracts per the Schwab adapter).
        current_iv: Weighted-average implied volatility today (as decimal,
            e.g., 0.45 for 45%).
        iv_52w_high: 52-week high IV (optional; used for IV rank).
        iv_52w_low: 52-week low IV (optional; used for IV rank).
        history_pc_ratios: Trailing 60d array of daily call/put volume ratios
            (oldest first). If None, pc_ratio sub-component defaults to 0.5.
        history_vol_oi: Trailing 60d array of daily volume/OI ratios
            (oldest first). If None, vol_oi sub-component defaults to 0.5.
        dominant_strike_vol_pct: Fraction of today's options volume on the
            single highest-volume strike. If > 0.70, hedging downweight applied.
        min_daily_volume: Minimum total contracts traded to use signal.
        min_oi: Minimum aggregate OI required.
        min_strikes: Minimum number of strikes with OI > 0.
        min_trade_sizes: Minimum distinct lot sizes required.
        iv_range_min_ppt: Minimum 52w IV range in percentage points for
            stable IV rank computation.

    Returns:
        OptionsSignalResult with composite=None if suppressed.
    """
    total_vol = total_call_vol + total_put_vol

    # ---- Mandatory safeguard checks ----
    if total_vol < min_daily_volume:
        logger.debug(
            "options_signal: suppressed — total_vol=%.0f < %d",
            total_vol, min_daily_volume,
        )
        return OptionsSignalResult(
            composite=None,
            pc_ratio_pctile=None, iv_rank=None, vol_oi_pctile=None,
            suppressed_reason=f"total_vol={total_vol:.0f} < {min_daily_volume}",
        )

    if total_oi < min_oi:
        logger.debug(
            "options_signal: suppressed — total_oi=%.0f < %d",
            total_oi, min_oi,
        )
        return OptionsSignalResult(
            composite=None,
            pc_ratio_pctile=None, iv_rank=None, vol_oi_pctile=None,
            suppressed_reason=f"total_oi={total_oi:.0f} < {min_oi}",
        )

    if strike_count_with_oi < min_strikes:
        logger.debug(
            "options_signal: suppressed — strike_count=%d < %d",
            strike_count_with_oi, min_strikes,
        )
        return OptionsSignalResult(
            composite=None,
            pc_ratio_pctile=None, iv_rank=None, vol_oi_pctile=None,
            suppressed_reason=f"strike_count={strike_count_with_oi} < {min_strikes}",
        )

    if distinct_lot_sizes < min_trade_sizes:
        logger.debug(
            "options_signal: suppressed — lot_sizes=%d < %d",
            distinct_lot_sizes, min_trade_sizes,
        )
        return OptionsSignalResult(
            composite=None,
            pc_ratio_pctile=None, iv_rank=None, vol_oi_pctile=None,
            suppressed_reason=(
                f"distinct_lot_sizes={distinct_lot_sizes} < {min_trade_sizes} "
                f"(possible block/hedging activity)"
            ),
        )

    # ---- Sub-component computation ----

    # 1. Call/put volume ratio percentile rank
    pc_ratio = total_call_vol / total_put_vol if total_put_vol > 0 else float("inf")
    if history_pc_ratios is not None and len(history_pc_ratios) >= 5:
        pc_pctile = _pctile_rank(pc_ratio, np.asarray(history_pc_ratios, dtype=float))
    else:
        pc_pctile = 0.5
        logger.debug("options_signal: pc_ratio history unavailable — defaulting to 0.5")

    # 2. IV rank
    iv_range_insufficient = False
    iv_rank: Optional[float] = None
    if current_iv is not None and iv_52w_high is not None and iv_52w_low is not None:
        iv_range = (iv_52w_high - iv_52w_low) * 100  # convert to percentage points
        if iv_range < iv_range_min_ppt:
            iv_rank = 0.5
            iv_range_insufficient = True
            logger.debug(
                "options_signal: IV range=%.2f pp < %.1f pp — iv_rank set to 0.5",
                iv_range, iv_range_min_ppt,
            )
        else:
            iv_rank = (current_iv - iv_52w_low) / (iv_52w_high - iv_52w_low)
            iv_rank = float(np.clip(iv_rank, 0.0, 1.0))
    else:
        iv_rank = 0.5
        logger.debug("options_signal: IV data unavailable — defaulting iv_rank to 0.5")

    # 3. Volume/OI percentile rank
    vol_oi_ratio = total_vol / total_oi if total_oi > 0 else 0.0
    if history_vol_oi is not None and len(history_vol_oi) >= 5:
        vol_oi_pctile = _pctile_rank(vol_oi_ratio, np.asarray(history_vol_oi, dtype=float))
    else:
        vol_oi_pctile = 0.5
        logger.debug("options_signal: vol/OI history unavailable — defaulting to 0.5")

    # ---- Composite (0.4 × pc + 0.3 × iv_rank + 0.3 × vol_oi) ----
    composite = 0.4 * pc_pctile + 0.3 * iv_rank + 0.3 * vol_oi_pctile

    # ---- Hedging filter: single-strike volume domination → halve composite ----
    hedging_downweighted = False
    if dominant_strike_vol_pct is not None and dominant_strike_vol_pct > 0.70:
        composite *= 0.5
        hedging_downweighted = True
        logger.debug(
            "options_signal: hedging downweight applied (dominant_strike=%.1f%%)",
            dominant_strike_vol_pct * 100,
        )

    logger.debug(
        "options_signal: pc=%.3f iv_rank=%.3f vol_oi=%.3f composite=%.3f",
        pc_pctile, iv_rank, vol_oi_pctile, composite,
    )

    return OptionsSignalResult(
        composite=float(np.clip(composite, 0.0, 1.0)),
        pc_ratio_pctile=pc_pctile,
        iv_rank=iv_rank,
        vol_oi_pctile=vol_oi_pctile,
        suppressed_reason=None,
        iv_range_insufficient=iv_range_insufficient,
        hedging_downweighted=hedging_downweighted,
    )
