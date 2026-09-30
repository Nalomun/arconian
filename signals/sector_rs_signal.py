"""
Sector Relative Strength signal.

Isolates firm-specific price action from sector-wide rotation by computing
the spread between a stock's 5-day return and its GICS sector ETF's 5-day
return, then percentile-ranking that spread against the stock's own trailing
60-day history of spreads.

  sector_spread_today  = stock_5d_return − sector_etf_5d_return
  sector_rs_percentile = percentile_rank(sector_spread_today, history_spreads)

A high percentile rank means the stock is outperforming its sector — a sign
of firm-specific information flow rather than broad sector rotation.

Returns 0.5 (neutral) if sector ETF data is unavailable or if history is
insufficient, per NULL propagation rules (Doc 3, Section 3.3).

NOTE: changed from z-score to percentile rank in v3 to maintain methodological
consistency with all other signals (distribution-free, no normality assumption).

Whitepaper reference: Section 3.2.4 (Relative Sector Strength)
Supplements reference: Doc 3, Section 3.4 (GICS sector → ETF mapping)
"""

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_MIN_HISTORY = 5   # minimum history entries before percentile rank is meaningful
_DEFAULT_NEUTRAL = 0.5


def _pctile_rank(value: float, history: np.ndarray) -> float:
    """Fraction of `history` values strictly below `value`. Range [0, 1]."""
    if len(history) == 0:
        return _DEFAULT_NEUTRAL
    return float(np.mean(history < value))


def compute_sector_rs_signal(
    stock_5d_return: Optional[float],
    etf_5d_return: Optional[float],
    history_spreads: Optional[np.ndarray],
) -> Optional[float]:
    """
    Compute the sector relative strength percentile rank.

    Args:
        stock_5d_return: The stock's 5-day cumulative return (e.g., 0.04 for
            +4%). None if price data is unavailable.
        etf_5d_return: The GICS sector ETF's 5-day cumulative return. None
            if sector mapping or ETF price data is unavailable.
        history_spreads: Trailing array of previous daily sector spread values
            (stock_5d_return − etf_5d_return for each prior day), oldest first.
            Should contain at least _MIN_HISTORY values for a meaningful rank.
            None if history is unavailable.

    Returns:
        Percentile rank in [0, 1]. Returns 0.5 (neutral) if any required
        data is missing, rather than None, because Sector RS is treated as
        a soft-NULL per Doc 3 Section 3.3 (only Options and Delay are
        hard-redistributed; Sector RS and Volume default to neutral).
    """
    # Missing sector ETF → neutral, no redistribution
    if stock_5d_return is None or etf_5d_return is None:
        logger.debug(
            "sector_rs: ETF/stock data unavailable (stock=%s etf=%s) — returning 0.5",
            stock_5d_return, etf_5d_return,
        )
        return _DEFAULT_NEUTRAL

    today_spread = stock_5d_return - etf_5d_return

    if history_spreads is None or len(history_spreads) < _MIN_HISTORY:
        logger.debug(
            "sector_rs: insufficient history (got %d, need ≥ %d) — returning 0.5",
            0 if history_spreads is None else len(history_spreads),
            _MIN_HISTORY,
        )
        return _DEFAULT_NEUTRAL

    history = np.asarray(history_spreads, dtype=float)
    rank = _pctile_rank(today_spread, history)

    logger.debug(
        "sector_rs: spread=%.4f rank=%.3f (history=%d)",
        today_spread, rank, len(history),
    )
    return rank


def compute_5d_return(prices: np.ndarray) -> Optional[float]:
    """
    Compute the 5-day cumulative return from a daily close price array.

    Args:
        prices: Array of closing prices in chronological order (oldest first).
            Needs at least 6 elements (today + 5 prior days).

    Returns:
        5-day return as a fraction (e.g., 0.04 for +4%), or None if
        insufficient data.
    """
    arr = np.asarray(prices, dtype=float)
    if len(arr) < 6:
        return None
    return float(arr[-1] / arr[-6] - 1.0)


def build_spread_history(
    stock_prices: np.ndarray,
    etf_prices: np.ndarray,
    lookback: int = 60,
) -> np.ndarray:
    """
    Build a trailing history of daily sector spread values for percentile ranking.

    For each day in the lookback window, computes the stock's rolling 5-day
    return minus the ETF's rolling 5-day return.

    Args:
        stock_prices: Daily close prices for the stock, oldest first. Must
            contain at least lookback + 5 values.
        etf_prices: Daily close prices for the sector ETF, aligned to the
            same dates. Same length as stock_prices.
        lookback: Number of daily spread values to return (default 60).

    Returns:
        1-D array of up to `lookback` spread values, oldest first.
    """
    s = np.asarray(stock_prices, dtype=float)
    e = np.asarray(etf_prices, dtype=float)

    min_len = min(len(s), len(e))
    if min_len < 6:
        return np.array([], dtype=float)

    if np.any(s[:min_len] <= 0) or np.any(e[:min_len] <= 0):
        return np.array([], dtype=float)

    # Compute rolling 5d return for each valid window end
    n_windows = min_len - 5
    stock_r5 = (s[5 : min_len] / s[:n_windows]) - 1.0
    etf_r5 = (e[5 : min_len] / e[:n_windows]) - 1.0
    spreads = stock_r5 - etf_r5

    # Return last `lookback` values
    return spreads[-lookback:]
