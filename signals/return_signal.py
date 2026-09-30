"""
Return Magnitude signal.

60-day absolute return percentile rank against the stock's own trailing history.
Direction is deliberately excluded here — large down moves and large up moves
both signal information arrival. The directional confirmation layer (Section 3.4)
determines which way to trade.

Returns None when fewer than _MIN_DAYS of history are available. Under normal
universe filters (≥ 60 trading days required), this should not occur in
production but is implemented defensively.

Whitepaper reference: Section 3.2.2 (Return Magnitude)
Supplements reference: Doc 3, Section 3.3 (NULL handling)
"""

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_WINDOW = 60
_MIN_DAYS = 20  # absolute floor; universe filter guarantees ≥ 60 in practice


def _pctile_rank(value: float, history: np.ndarray) -> float:
    """Fraction of `history` values strictly below `value`. Range [0, 1]."""
    if len(history) == 0:
        return 0.5
    return float(np.mean(history < value))


def compute_return_signal(
    today_abs_return: float,
    history_abs_returns: np.ndarray,
) -> Optional[float]:
    """
    Compute the 60-day absolute return percentile rank.

    Args:
        today_abs_return: Absolute value of today's 1-day return (e.g., 0.035
            for a 3.5% move in either direction). Must be non-negative.
        history_abs_returns: Array of trailing daily absolute returns in
            chronological order (oldest first), NOT including today.
            Use 60+ observations where available; at least _MIN_DAYS required.

    Returns:
        Percentile rank in [0, 1], or None if fewer than _MIN_DAYS of history.
    """
    history = np.asarray(history_abs_returns, dtype=float)
    n = len(history)

    if n < _MIN_DAYS:
        logger.debug(
            "return_signal: insufficient history (%d days, need ≥ %d) — returning None",
            n, _MIN_DAYS,
        )
        return None

    window = history[-_WINDOW:]
    rank = _pctile_rank(float(today_abs_return), window)

    logger.debug(
        "return_signal: abs_return=%.4f rank=%.3f (window=%d)",
        today_abs_return, rank, len(window),
    )
    return rank
