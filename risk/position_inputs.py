"""
Per-ticker risk inputs — sub-industry + 60-day returns (#18 follow-on).

``RiskEngine.approve_new_position`` enforces a GICS sub-industry hard limit and
a Ledoit-Wolf correlation-cluster limit, which need a sub_industry string (GICS
proxy = yfinance 'industry') and a 60-day daily-return array for the candidate
*and* every open position. The initial #18 wiring left both None, so those two
constraints stayed inactive ("sector_etf now, enrich later"). This module fills
them via the existing YFinanceAdapter, degrading to None on any failure so the
relevant constraint simply stays inactive for that name rather than crashing.
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 3 months ≈ 63 trading days — enough for a 60-day return window with margin.
_RETURNS_PERIOD = "3mo"
_RETURNS_WINDOW = 60
# Ledoit-Wolf needs a handful of observations to be meaningful.
_MIN_RETURNS = 4


def fetch_sub_industry(yf_adapter, ticker: str) -> Optional[str]:
    """Return the yfinance 'industry' (GICS sub-industry proxy), or None."""
    try:
        info = yf_adapter.get_ticker_info(ticker)
    except Exception:
        logger.debug("sub_industry fetch failed for %s", ticker, exc_info=True)
        return None
    return getattr(info, "industry", None) if info is not None else None


def fetch_returns_60d(yf_adapter, ticker: str) -> Optional[np.ndarray]:
    """Return the last ~60 daily returns for ``ticker`` as an array, or None."""
    try:
        prices = yf_adapter.get_daily_prices([ticker], period=_RETURNS_PERIOD)
    except Exception:
        logger.debug("returns_60d fetch failed for %s", ticker, exc_info=True)
        return None
    if prices is None or getattr(prices, "empty", True):
        return None
    try:
        if isinstance(prices.columns, pd.MultiIndex):
            close = prices["Close"][ticker]
        else:
            close = prices["Close"]
        close = close.dropna()
    except (KeyError, TypeError):
        return None

    rets = close.pct_change().dropna().to_numpy(dtype=float)
    if len(rets) < _MIN_RETURNS:
        return None
    return rets[-_RETURNS_WINDOW:]


def fetch_position_risk_inputs(yf_adapter, ticker: str) -> tuple[Optional[str], Optional[np.ndarray]]:
    """Return ``(sub_industry, returns_60d)`` for a ticker (each None on failure)."""
    if yf_adapter is None:
        return None, None
    return fetch_sub_industry(yf_adapter, ticker), fetch_returns_60d(yf_adapter, ticker)
