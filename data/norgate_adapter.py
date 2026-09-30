"""
Norgate Data adapter — historical/backtest use only.

Wraps the `norgatedata` Python package which requires:
  - Windows OS
  - Norgate Data Updater (NDU) running in the background
  - Active Norgate subscription (Platinum tier for delisted symbols)

This adapter will raise NorgateUnavailableError on construction if:
  - `norgatedata` is not installed
  - NDU is not running / data is inaccessible

All methods return None on per-symbol failures rather than raising.

NOTE: This adapter is NEVER used for live signal computation.
      Live signals use SchwabAdapter for market data.
      NorgateAdapter is used exclusively for:
        - Survivorship-bias-free backtesting
        - Historical delay score computation
        - Point-in-time universe construction

Whitepaper reference: Section 6 (Backtesting Infrastructure)
Supplements reference: Doc 1, Section 1.6
"""

import logging
import sys
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


class NorgateUnavailableError(RuntimeError):
    """Raised when Norgate Data is unavailable (NDU not running or not installed)."""
    pass


class NorgateAdapter:
    """
    Adapter for Norgate Data historical price database.

    Construction validates that norgatedata is installed and NDU is running.
    Raises NorgateUnavailableError if either condition is not met.

    Args:
        price_adjustment: Stock price adjustment type.
            'totalreturn' (default) — adjusted for dividends and splits.
            'none' — unadjusted prices.
    """

    def __init__(self, price_adjustment: str = "totalreturn") -> None:
        self._norgatedata = self._import_or_raise()
        self._validate_connection()

        _adj_map = {
            "totalreturn": self._norgatedata.StockPriceAdjustmentType.TOTALRETURN,
            "none": self._norgatedata.StockPriceAdjustmentType.NONE,
            "capital": self._norgatedata.StockPriceAdjustmentType.CAPITAL,
        }
        if price_adjustment not in _adj_map:
            raise ValueError(
                f"price_adjustment must be one of {list(_adj_map)}; got {price_adjustment!r}"
            )
        self._adjustment = _adj_map[price_adjustment]
        logger.info("NorgateAdapter initialised (adjustment=%s)", price_adjustment)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_price_history(
        self,
        symbol: str,
        start: date,
        end: date,
    ):
        """
        Return daily OHLCV price history for a symbol.

        Args:
            symbol: Ticker symbol (active or delisted).
            start: Start date (inclusive).
            end: End date (inclusive).

        Returns:
            pandas DataFrame with columns [Open, High, Low, Close, Volume, Turnover]
            indexed by Date, or None on failure.
        """
        try:
            df = self._norgatedata.price_timeseries(
                symbol,
                stock_price_adjustment_setting=self._adjustment,
                padding_setting=self._norgatedata.PaddingType.NONE,
                start_date=start.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
                timeseriesformat="pandas-dataframe",
            )
            if df is None or df.empty:
                logger.debug("Norgate: no data for %s %s–%s", symbol, start, end)
                return None
            return df
        except Exception as exc:
            logger.warning("Norgate price history failed for %s: %s", symbol, exc)
            return None

    def get_all_symbols(self, include_delisted: bool = True) -> Optional[list[str]]:
        """
        Return the full list of available symbols, optionally including delisted.

        Args:
            include_delisted: If True, merge active and delisted databases.

        Returns:
            List of ticker strings, or None on failure.
        """
        try:
            active = self._norgatedata.database_symbols("US Equities") or []
            if not include_delisted:
                return list(active)
            delisted = self._norgatedata.database_symbols("US Equities Delisted") or []
            return list(set(active + delisted))
        except Exception as exc:
            logger.warning("Norgate get_all_symbols failed: %s", exc)
            return None

    def get_index_constituents(
        self,
        symbol: str,
        index_name: str,
        as_of: date,
    ) -> Optional[bool]:
        """
        Check whether a symbol was a constituent of an index on a given date.

        Args:
            symbol: Ticker symbol.
            index_name: e.g. 'Russell 3000', 'S&P 500'.
            as_of: Date to check constituent status.

        Returns:
            True if constituent on that date, False if not, None on error.
        """
        try:
            df = self._norgatedata.index_constituent_timeseries(
                symbol,
                index_name,
                timeseriesformat="pandas-dataframe",
            )
            if df is None or df.empty:
                return False
            # Find the row closest to as_of
            date_str = as_of.strftime("%Y-%m-%d")
            row = df[df.index <= date_str]
            if row.empty:
                return False
            return bool(row.iloc[-1].iloc[0])
        except Exception as exc:
            logger.warning(
                "Norgate index_constituents failed for %s / %s: %s", symbol, index_name, exc
            )
            return None

    def get_market_cap(self, symbol: str) -> Optional[float]:
        """
        Return the most recent market cap (millions USD) for a symbol.

        Args:
            symbol: Ticker symbol.

        Returns:
            Market cap in millions, or None on failure.
        """
        try:
            value, _ = self._norgatedata.fundamental(symbol, "marketcap")
            if value is None:
                return None
            return float(value) / 1_000_000  # Norgate returns in dollars
        except Exception as exc:
            logger.warning("Norgate market_cap failed for %s: %s", symbol, exc)
            return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _import_or_raise():
        """Import norgatedata or raise NorgateUnavailableError."""
        try:
            import norgatedata
            return norgatedata
        except ImportError:
            raise NorgateUnavailableError(
                "norgatedata package is not installed. "
                "Install with: pip install norgatedata  (Windows only). "
                "See README for setup instructions."
            )

    def _validate_connection(self) -> None:
        """Confirm NDU is running by making a lightweight API call."""
        if sys.platform != "win32":
            raise NorgateUnavailableError(
                "Norgate Data Updater (NDU) is a Windows-only application. "
                "NorgateAdapter cannot be used on non-Windows systems."
            )
        try:
            # A harmless call that will fail if NDU is not running
            self._norgatedata.database_symbols("US Equities")
        except Exception as exc:
            raise NorgateUnavailableError(
                f"Cannot connect to Norgate Data Updater (NDU). "
                f"Ensure NDU is running before using NorgateAdapter. "
                f"Original error: {exc}"
            ) from exc
