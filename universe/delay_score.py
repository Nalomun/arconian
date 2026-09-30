"""
Price Delay Score — Hou & Moskowitz (2005) implementation.

Measures how slowly a stock incorporates market-wide information into its
price. Stocks with high delay have returns that correlate more strongly with
LAGGED market returns than with contemporaneous ones, indicating unexploited
predictability.

Method:
  Restricted model:  r_i,t = α + β₀·r_m,t + ε
  Full model:        r_i,t = α + Σ(k=0..4) βₖ·r_m,t−k + ε

  Delay score = 1 − (R²_restricted / R²_full)
  Range: [0, 1] — higher score = slower information incorporation

Requirements:
  - ≥52 weeks of weekly returns for both the stock and market proxy (SPY)
  - Returns None if insufficient data

Usage:
  score = compute_delay_score(stock_weekly_returns, market_weekly_returns)
  all_scores = compute_all_delay_scores(["AAPL", "SOFI"], yf_adapter)

Whitepaper reference: Section 2.3 (Price Delay Signal)
"""

import logging
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Market proxy for delay score computation
_MARKET_PROXY = "SPY"
_MIN_WEEKS = 52
_NUM_LAGS = 4


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_delay_score(
    stock_returns: np.ndarray,
    market_returns: np.ndarray,
    min_weeks: int = _MIN_WEEKS,
    num_lags: int = _NUM_LAGS,
) -> Optional[float]:
    """
    Compute the Hou-Moskowitz price delay score.

    Args:
        stock_returns: Weekly return series for the stock (aligned with market).
        market_returns: Weekly return series for the market proxy (same length).
        min_weeks: Minimum observations required after lagging. Default 52.
        num_lags: Number of lagged market return regressors. Default 4.

    Returns:
        Delay score in [0, 1], or None if insufficient data.
    """
    if len(stock_returns) != len(market_returns):
        raise ValueError(
            f"stock_returns ({len(stock_returns)}) and market_returns "
            f"({len(market_returns)}) must have the same length."
        )

    n = len(stock_returns)
    if n < min_weeks + num_lags:
        logger.debug(
            "Insufficient data for delay score: %d weeks (need %d)",
            n, min_weeks + num_lags,
        )
        return None

    # Trim to usable window (lags consume the first num_lags observations)
    y = stock_returns[num_lags:].astype(float)
    m = market_returns.astype(float)

    # Restricted model: [1, r_m,t]
    X_restricted = np.column_stack([np.ones(len(y)), m[num_lags:]])

    # Full model: [1, r_m,t, r_m,t-1, ..., r_m,t-num_lags]
    lag_cols = [m[num_lags - k : len(m) - k] for k in range(num_lags + 1)]
    X_full = np.column_stack([np.ones(len(y))] + lag_cols)

    r2_restricted = _ols_r2(X_restricted, y)
    r2_full = _ols_r2(X_full, y)

    if r2_full is None or r2_restricted is None:
        return None

    # Guard: if full model explains nothing, delay is undefined
    if r2_full <= 0.0:
        return 0.0

    delay = 1.0 - (r2_restricted / r2_full)
    # Clip to [0, 1] — floating point edge cases can push slightly outside
    return float(np.clip(delay, 0.0, 1.0))


def _ols_r2(X: np.ndarray, y: np.ndarray) -> Optional[float]:
    """
    Compute R² for an OLS regression y ~ X.

    Args:
        X: Design matrix (rows = observations, cols = regressors incl. intercept).
        y: Target vector.

    Returns:
        R² in [0, 1], or None if the regression cannot be solved.
    """
    try:
        coeffs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        y_hat = X @ coeffs
        ss_res = float(np.sum((y - y_hat) ** 2))
        ss_tot = float(np.sum((y - np.mean(y)) ** 2))
        if ss_tot == 0.0:
            return 0.0
        return max(0.0, 1.0 - ss_res / ss_tot)
    except np.linalg.LinAlgError as exc:
        logger.warning("OLS failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Weekly return conversion
# ---------------------------------------------------------------------------

def daily_to_weekly_returns(
    daily_close: pd.Series,
    min_days_per_week: int = 3,
) -> pd.Series:
    """
    Convert a daily close price series to weekly returns.

    Uses Friday (or last trading day of week) closes. Weeks with fewer
    than min_days_per_week trading days are dropped to avoid partial weeks
    distorting the return.

    Args:
        daily_close: Pandas Series indexed by DatetimeIndex with daily closes.
        min_days_per_week: Weeks with fewer days are excluded.

    Returns:
        Weekly return series (percent change close-to-close).
    """
    weekly = daily_close.resample("W-FRI").agg(
        lambda x: x.iloc[-1] if len(x) >= min_days_per_week else np.nan
    ).dropna()
    returns = weekly.pct_change().dropna()
    return returns


# ---------------------------------------------------------------------------
# Batch computation
# ---------------------------------------------------------------------------

def compute_all_delay_scores(
    tickers: list[str],
    yf_adapter,                  # YFinanceAdapter — avoid circular import
    market_proxy: str = _MARKET_PROXY,
    lookback_period: str = "2y",
) -> dict[str, Optional[float]]:
    """
    Compute delay scores for a batch of tickers.

    Fetches ~2 years of daily data via yfinance to ensure ≥52 weekly
    observations. Uses SPY as the market proxy.

    Args:
        tickers: List of ticker symbols to score.
        yf_adapter: YFinanceAdapter instance.
        market_proxy: Market return proxy ticker (default: 'SPY').
        lookback_period: yfinance period string for historical data.

    Returns:
        Dict mapping ticker → delay score (or None if insufficient data).
    """
    if not tickers:
        return {}

    all_symbols = list(set(tickers + [market_proxy]))
    df = yf_adapter.get_daily_prices(all_symbols, period=lookback_period)

    if df is None or df.empty:
        logger.error("delay_score: no price data returned for batch of %d tickers", len(tickers))
        return {t: None for t in tickers}

    # Extract close prices; handle MultiIndex from yf.download
    if isinstance(df.columns, pd.MultiIndex):
        closes = df["Close"]
    else:
        closes = df

    if market_proxy not in closes.columns:
        logger.error("delay_score: market proxy %s not in price data", market_proxy)
        return {t: None for t in tickers}

    market_weekly = daily_to_weekly_returns(closes[market_proxy])
    market_arr = market_weekly.values

    results: dict[str, Optional[float]] = {}
    for ticker in tickers:
        if ticker not in closes.columns:
            logger.debug("delay_score: no price data for %s", ticker)
            results[ticker] = None
            continue

        stock_weekly = daily_to_weekly_returns(closes[ticker])

        # Align on common dates
        aligned = pd.concat([stock_weekly, market_weekly], axis=1, join="inner").dropna()
        if len(aligned) < _MIN_WEEKS + _NUM_LAGS:
            logger.debug(
                "delay_score: only %d aligned weekly obs for %s (need %d)",
                len(aligned), ticker, _MIN_WEEKS + _NUM_LAGS,
            )
            results[ticker] = None
            continue

        score = compute_delay_score(
            aligned.iloc[:, 0].values,
            aligned.iloc[:, 1].values,
        )
        results[ticker] = score
        logger.debug("delay_score: %s = %.4f", ticker, score if score is not None else float("nan"))

    return results
