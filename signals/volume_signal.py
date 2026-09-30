"""
Volume Surprise signal — Gervais, Kaniel & Mingelgrin (2001).

Multi-window percentile rank of today's dollar volume against 20d, 60d,
and 120d trailing histories. Averaged into a single composite.

Windows used depend on available history:
  ≥ 120 days : all three windows (full)
  60–119 days: 20d and 60d only  → flagged short_history=True
  20–59 days : 20d only           → flagged short_history=True
  < 20 days  : composite = None

Whitepaper reference: Section 3.2.1 (Volume Surprise)
Supplements reference: Doc 3, Section 3.3 (NULL handling for short history)
"""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_WINDOW_20 = 20
_WINDOW_60 = 60
_WINDOW_120 = 120


def _pctile_rank(value: float, history: np.ndarray) -> float:
    """Fraction of `history` values strictly below `value`. Range [0, 1]."""
    if len(history) == 0:
        return 0.5
    return float(np.mean(history < value))


@dataclass
class VolumeSignalResult:
    """
    Result of the volume surprise computation for a single ticker.

    All percentile ranks are in [0, 1] or None if the window is unavailable.
    `composite` is the mean of all available window ranks.
    `short_history` is True when the 120d window could not be used.
    """

    pctile_20d: Optional[float]
    pctile_60d: Optional[float]
    pctile_120d: Optional[float]
    composite: Optional[float]   # mean of available windows; None if < 20 days
    short_history: bool          # True if 120d window unavailable (< 120 days of data)


def compute_volume_signal(
    today_dollar_vol: float,
    history_dollar_vol: np.ndarray,
) -> VolumeSignalResult:
    """
    Compute the multi-window volume percentile rank.

    Args:
        today_dollar_vol: Dollar volume for the current trading day
            (price × shares; in the same units as history).
        history_dollar_vol: Array of daily dollar volumes in chronological
            order (oldest first), NOT including today. Use the trailing
            120+ days if available.

    Returns:
        VolumeSignalResult with all available window ranks and composite.
    """
    history = np.asarray(history_dollar_vol, dtype=float)
    n = len(history)

    p20 = p60 = p120 = None
    short_history = True

    if n >= _WINDOW_20:
        p20 = _pctile_rank(today_dollar_vol, history[-_WINDOW_20:])
    if n >= _WINDOW_60:
        p60 = _pctile_rank(today_dollar_vol, history[-_WINDOW_60:])
    if n >= _WINDOW_120:
        p120 = _pctile_rank(today_dollar_vol, history[-_WINDOW_120:])
        short_history = False

    available = [v for v in (p20, p60, p120) if v is not None]
    composite = float(np.mean(available)) if available else None

    logger.debug(
        "volume_signal: p20=%.3f p60=%s p120=%s composite=%s short=%s",
        p20 or 0,
        f"{p60:.3f}" if p60 is not None else "n/a",
        f"{p120:.3f}" if p120 is not None else "n/a",
        f"{composite:.3f}" if composite is not None else "None",
        short_history,
    )

    return VolumeSignalResult(
        pctile_20d=p20,
        pctile_60d=p60,
        pctile_120d=p120,
        composite=composite,
        short_history=short_history,
    )
