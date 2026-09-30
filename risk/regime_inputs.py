"""
Market-regime inputs — VIX level + IWM rolling returns for the RiskEngine.

Tier-3 #18: ``RiskEngine.current_regime`` needs the current VIX level and IWM
trailing 10-day / 20-day returns, but nothing in the system fetched them, so the
regime gate could never run. This module pulls both series via the existing
``YFinanceAdapter`` and computes the returns, degrading gracefully: it returns
None on any fetch/short-history failure so callers can fall back to a documented
default (NORMAL) rather than crash the scan.

Whitepaper reference: Section 4.1 (Market Regime Detection)
"""

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_VIX = "^VIX"
_IWM = "IWM"
# 3 months ≈ 63 trading days — comfortably more than the 21 closes the 20-day
# return needs, with margin for missing rows.
_FETCH_PERIOD = "3mo"


@dataclass
class RegimeInputs:
    """Inputs to RiskEngine.current_regime()."""

    vix: float
    iwm_10d_return: float
    iwm_20d_return: float


def _close_series(prices: pd.DataFrame, symbol: str) -> Optional[pd.Series]:
    """Extract a NaN-dropped Close series for ``symbol`` from a yfinance frame.

    Handles both the MultiIndex frame yfinance returns for multi-symbol
    downloads and the single-level frame for one symbol.
    """
    try:
        if isinstance(prices.columns, pd.MultiIndex):
            series = prices["Close"][symbol]
        else:
            series = prices["Close"]
        return series.dropna()
    except (KeyError, TypeError):
        return None


def fetch_regime_inputs(yf_adapter) -> Optional[RegimeInputs]:
    """
    Fetch VIX + IWM and compute regime inputs.

    Args:
        yf_adapter: YFinanceAdapter (or anything with the same
            ``get_daily_prices(symbols, period)`` contract).

    Returns:
        RegimeInputs, or None if the data could not be fetched or there were
        fewer than 21 IWM closes (so the 20-day return can't be computed).
    """
    prices = yf_adapter.get_daily_prices([_VIX, _IWM], period=_FETCH_PERIOD)
    if prices is None or getattr(prices, "empty", True):
        logger.warning("regime_inputs: VIX/IWM fetch failed — regime unknown")
        return None

    vix_close = _close_series(prices, _VIX)
    iwm_close = _close_series(prices, _IWM)
    if vix_close is None or iwm_close is None or len(vix_close) < 1 or len(iwm_close) < 21:
        logger.warning(
            "regime_inputs: insufficient VIX/IWM history "
            "(vix=%s iwm=%s) — regime unknown",
            None if vix_close is None else len(vix_close),
            None if iwm_close is None else len(iwm_close),
        )
        return None

    # 10/20 *trading*-day returns: last close vs the close 11/21 bars back.
    vix = float(vix_close.iloc[-1])
    iwm_10d = float(iwm_close.iloc[-1] / iwm_close.iloc[-11] - 1.0)
    iwm_20d = float(iwm_close.iloc[-1] / iwm_close.iloc[-21] - 1.0)
    logger.debug(
        "regime_inputs: VIX=%.1f IWM 10d=%.2f%% 20d=%.2f%%",
        vix, 100 * iwm_10d, 100 * iwm_20d,
    )
    return RegimeInputs(vix=vix, iwm_10d_return=iwm_10d, iwm_20d_return=iwm_20d)
