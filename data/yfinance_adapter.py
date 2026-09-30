"""
yfinance data adapter.

Provides daily OHLCV, ticker info (sector, market cap, short interest),
earnings dates, and sector ETF prices. Used as the primary supplementary
data source and as the live-signal fallback when Schwab is unavailable.

Caching:
  - ticker_info: 7-day TTL (market cap and sector don't change intraday)
  - sector ETF prices: 24-hour TTL (refreshed daily at 4:15 PM ET)

All methods return None on failure — never raise to callers.

Whitepaper reference: Section 2 (Signal Design — sector RS, ticker info)
Supplements reference: Doc 1, Section 1.4 and 1.5; Doc 3, Section 3.4
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

from data.utils import FileCache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GICS / yfinance sector maps (Doc 3, Section 3.4)
# ---------------------------------------------------------------------------

SECTOR_ETFS: list[str] = [
    "XLK", "XLV", "XLF", "XLY", "XLC",
    "XLI", "XLP", "XLE", "XLU", "XLRE", "XLB",
]

GICS_SECTOR_ETF_MAP: dict[str, str] = {
    "Information Technology": "XLK",
    "Health Care":            "XLV",
    "Financials":             "XLF",
    "Consumer Discretionary": "XLY",
    "Communication Services": "XLC",
    "Industrials":            "XLI",
    "Consumer Staples":       "XLP",
    "Energy":                 "XLE",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Materials":              "XLB",
}

# yfinance sector names → GICS canonical names
YFINANCE_SECTOR_NORMALIZE: dict[str, str] = {
    "Technology":             "Information Technology",
    "Healthcare":             "Health Care",
    "Financial Services":     "Financials",
    "Consumer Cyclical":      "Consumer Discretionary",
    "Communication Services": "Communication Services",
    "Industrials":            "Industrials",
    "Consumer Defensive":     "Consumer Staples",
    "Energy":                 "Energy",
    "Utilities":              "Utilities",
    "Real Estate":            "Real Estate",
    "Basic Materials":        "Materials",
}


def normalize_sector(yfinance_sector: Optional[str]) -> Optional[str]:
    """Map a yfinance sector string to the GICS canonical sector name."""
    if yfinance_sector is None:
        return None
    return YFINANCE_SECTOR_NORMALIZE.get(yfinance_sector)


def get_sector_etf(yfinance_sector: Optional[str]) -> Optional[str]:
    """
    Map a yfinance sector string to the corresponding SPDR sector ETF ticker.

    Args:
        yfinance_sector: Raw sector string from yfinance .info dict.

    Returns:
        ETF ticker string (e.g. 'XLK') or None if unmappable.
    """
    gics = normalize_sector(yfinance_sector)
    if gics is None:
        if yfinance_sector:
            logger.warning("Unknown yfinance sector: %r", yfinance_sector)
        return None
    return GICS_SECTOR_ETF_MAP.get(gics)


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------

@dataclass
class TickerInfo:
    """Fundamental and classification data for a single ticker."""

    ticker: str
    sector_yfinance: Optional[str]          # raw yfinance string
    sector_gics: Optional[str]              # normalized GICS sector
    sector_etf: Optional[str]               # e.g. 'XLK'
    market_cap_mm: Optional[float]          # millions USD
    short_pct_float: Optional[float]        # 0–1 fraction
    shares_outstanding: Optional[float]
    industry: Optional[str]                 # GICS sub-industry (approximate)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

# TTL constants
_TICKER_INFO_TTL = 7 * 24 * 3600   # 7 days
_SECTOR_ETF_TTL  = 24 * 3600        # 24 hours


class YFinanceAdapter:
    """
    Adapter for yfinance market data.

    Args:
        cache_dir: Directory for file cache. Defaults to '.cache'.
    """

    def __init__(self, cache_dir: str = ".cache") -> None:
        try:
            import yfinance as yf
            self._yf = yf
        except ImportError as exc:
            raise ImportError(
                "yfinance is not installed. Run: pip install yfinance"
            ) from exc
        self._cache = FileCache(cache_dir)
        logger.info("YFinanceAdapter initialised")

    # ------------------------------------------------------------------
    # Price history
    # ------------------------------------------------------------------

    def get_daily_prices(
        self,
        symbols: list[str],
        period: str = "1y",
    ) -> Optional[pd.DataFrame]:
        """
        Return daily OHLCV for one or more symbols.

        Uses yf.download() for batched fetching. Returns a MultiIndex
        DataFrame if multiple symbols, or a single-level DataFrame for one.

        Args:
            symbols: List of ticker strings.
            period: yfinance period string ('1y', '2y', '6mo', etc.).

        Returns:
            DataFrame with DatetimeIndex and OHLCV columns, or None on failure.
        """
        if not symbols:
            return None
        try:
            df = self._yf.download(
                symbols,
                period=period,
                auto_adjust=True,
                progress=False,
            )
            if df.empty:
                logger.warning("yfinance returned empty prices for %s", symbols)
                return None
            return df
        except Exception as exc:
            logger.error("yfinance get_daily_prices failed for %s: %s", symbols, exc)
            return None

    # ------------------------------------------------------------------
    # Ticker info
    # ------------------------------------------------------------------

    def get_ticker_info(self, symbol: str) -> Optional[TickerInfo]:
        """
        Return fundamental and classification data for a single ticker.

        Results are cached for 7 days.

        Args:
            symbol: Ticker string.

        Returns:
            TickerInfo dataclass or None on failure.
        """
        cache_key = f"ticker_info_{symbol}"
        cached = self._cache.get(cache_key, ttl_seconds=_TICKER_INFO_TTL)
        if cached is not None:
            return TickerInfo(**cached)

        try:
            info = self._yf.Ticker(symbol).info
        except Exception as exc:
            logger.warning("yfinance ticker info failed for %s: %s", symbol, exc)
            return None

        if not info or "symbol" not in info:
            logger.warning("yfinance returned empty info for %s", symbol)
            return None

        mktcap = info.get("marketCap")
        result = TickerInfo(
            ticker=symbol,
            sector_yfinance=info.get("sector"),
            sector_gics=normalize_sector(info.get("sector")),
            sector_etf=get_sector_etf(info.get("sector")),
            market_cap_mm=round(mktcap / 1_000_000, 2) if mktcap else None,
            short_pct_float=info.get("shortPercentOfFloat"),
            shares_outstanding=info.get("sharesOutstanding"),
            industry=info.get("industry"),
        )

        # Cache as dict so FileCache can serialise it
        self._cache.set(cache_key, {
            "ticker": result.ticker,
            "sector_yfinance": result.sector_yfinance,
            "sector_gics": result.sector_gics,
            "sector_etf": result.sector_etf,
            "market_cap_mm": result.market_cap_mm,
            "short_pct_float": result.short_pct_float,
            "shares_outstanding": result.shares_outstanding,
            "industry": result.industry,
        })
        return result

    # ------------------------------------------------------------------
    # Earnings dates
    # ------------------------------------------------------------------

    def get_earnings_dates(self, symbol: str) -> Optional[list[date]]:
        """
        Return a list of known earnings dates for a symbol, sorted ascending.

        Primary source: yfinance .earnings_dates property.

        Args:
            symbol: Ticker string.

        Returns:
            List of date objects, or None if completely unavailable.
        """
        try:
            ticker = self._yf.Ticker(symbol)
            df = ticker.earnings_dates
            if df is None or df.empty:
                logger.debug("No earnings dates from yfinance for %s", symbol)
                return None
            dates = [d.date() if hasattr(d, "date") else d for d in df.index]
            return sorted(d for d in dates if d is not None)
        except Exception as exc:
            logger.warning("yfinance earnings_dates failed for %s: %s", symbol, exc)
            return None

    # ------------------------------------------------------------------
    # Sector ETF prices
    # ------------------------------------------------------------------

    def get_sector_etf_prices(self, period: str = "3mo") -> Optional[pd.DataFrame]:
        """
        Return daily close prices for all 11 SPDR sector ETFs.

        Results cached for 24 hours. The returned DataFrame has ETF symbols
        as columns and dates as the index.

        Args:
            period: yfinance period string. '3mo' gives ~63 trading days,
                    sufficient for the 60-day Sector RS window.

        Returns:
            DataFrame of daily closes, or None on failure.
        """
        cache_key = f"sector_etf_prices_{period}"
        cached = self._cache.get(cache_key, ttl_seconds=_SECTOR_ETF_TTL)
        if cached is not None:
            try:
                return pd.read_json(cached)
            except Exception:
                pass  # Cache corrupt — fall through to re-fetch

        try:
            df = self._yf.download(
                SECTOR_ETFS,
                period=period,
                auto_adjust=True,
                progress=False,
            )
            if df.empty:
                logger.warning("yfinance returned empty sector ETF prices")
                return None

            # Extract Close prices; result is a DataFrame with ETF tickers as columns
            if isinstance(df.columns, pd.MultiIndex):
                closes = df["Close"]
            else:
                closes = df[["Close"]] if "Close" in df.columns else df

            self._cache.set(cache_key, closes.to_json())
            return closes
        except Exception as exc:
            logger.error("yfinance get_sector_etf_prices failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Price history (specific date range)
    # ------------------------------------------------------------------

    def get_price_history(
        self,
        ticker: str,
        start: str,
        end: str,
        interval: str = "1d",
    ) -> Optional[pd.DataFrame]:
        """
        Return OHLCV history for a single ticker between start and end dates.

        Args:
            ticker: Ticker string.
            start: Start date 'YYYY-MM-DD' (inclusive).
            end: End date 'YYYY-MM-DD' (exclusive per yfinance convention).
            interval: Data interval ('1d', '5m', etc.).

        Returns:
            DataFrame with DatetimeIndex and OHLCV columns, or None on failure.
        """
        try:
            df = self._yf.download(
                ticker,
                start=start,
                end=end,
                interval=interval,
                auto_adjust=True,
                progress=False,
            )
            if df.empty:
                logger.warning(
                    "yfinance returned empty history for %s (%s to %s)", ticker, start, end
                )
                return None
            return df
        except Exception as exc:
            logger.error("yfinance get_price_history failed for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------
    # Current price
    # ------------------------------------------------------------------

    def get_latest_price(self, symbol: str) -> Optional[float]:
        """
        Return the most recent traded price (real-time or 15-min delayed).

        Uses fast_info which is a lightweight quote endpoint — no full info
        dict fetch needed.

        Args:
            symbol: Ticker string.

        Returns:
            Float price, or None on failure.
        """
        try:
            price = self._yf.Ticker(symbol).fast_info["lastPrice"]
            return float(price) if price is not None else None
        except Exception as exc:
            logger.warning("yfinance get_latest_price failed for %s: %s", symbol, exc)
            return None
