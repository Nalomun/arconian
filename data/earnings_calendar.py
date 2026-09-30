"""
Earnings calendar — multi-source earnings date lookup.

Primary source: yfinance earnings_dates.
Secondary/validation: Alpha Vantage bulk CSV download (if API key configured).

When both sources are available and they disagree, the more conservative
(earlier) date is used for exclusion zone purposes.

Returns None if the earnings date is unknown — callers must handle None.
None is not a failure: some tickers genuinely have no upcoming earnings
(e.g., ETFs, foreign private issuers with different reporting cycles).

Whitepaper reference: Section 2.4 (Earnings Exclusion)
Supplements reference: Doc 1, Sections 1.5 and 1.7
"""

import csv
import io
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np
import requests

from data.utils import FileCache
from data.yfinance_adapter import YFinanceAdapter

logger = logging.getLogger(__name__)

_ALPHA_VANTAGE_TTL = 24 * 3600   # Cache Alpha Vantage bulk download for 24 hours
_YFINANCE_TTL = 6 * 3600          # Cache per-ticker yfinance lookups for 6 hours


def _trading_days_between(start: date, end: date) -> int:
    """
    Count NYSE weekday sessions in the half-open interval [start, end).

    Uses numpy.busday_count (Mon–Fri). For an earnings date in the future,
    _trading_days_between(today, earnings) is the trading days until it; for a
    past date, _trading_days_between(earnings, today) is the trading days since.
    Weekends are excluded; holidays are not (negligible for a ±1–3 day window).
    """
    return int(np.busday_count(start, end))


class EarningsCalendar:
    """
    Multi-source earnings date provider.

    Args:
        yf_adapter: YFinanceAdapter instance (required).
        alpha_vantage_key: Alpha Vantage API key for cross-validation.
            If not provided, yfinance is the sole source.
        cache_dir: Directory for file cache.
    """

    def __init__(
        self,
        yf_adapter: YFinanceAdapter,
        alpha_vantage_key: Optional[str] = None,
        cache_dir: str = ".cache",
    ) -> None:
        self._yf = yf_adapter
        self._av_key = alpha_vantage_key or os.environ.get("ALPHA_VANTAGE_API_KEY")
        self._cache = FileCache(cache_dir)
        self._av_bulk: Optional[dict[str, list[date]]] = None  # loaded lazily
        logger.info(
            "EarningsCalendar initialised (Alpha Vantage: %s)",
            "configured" if self._av_key else "not configured",
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_next_earnings(self, symbol: str) -> Optional[date]:
        """
        Return the next upcoming earnings date for a symbol.

        Args:
            symbol: Ticker string.

        Returns:
            Next earnings date, or None if unknown.
        """
        today = date.today()
        all_dates = self._get_all_dates(symbol)
        if not all_dates:
            return None
        future = [d for d in all_dates if d >= today]
        return min(future) if future else None

    def get_last_earnings(self, symbol: str) -> Optional[date]:
        """
        Return the most recent past earnings date for a symbol.

        Args:
            symbol: Ticker string.

        Returns:
            Most recent past earnings date, or None if unknown.
        """
        today = date.today()
        all_dates = self._get_all_dates(symbol)
        if not all_dates:
            return None
        past = [d for d in all_dates if d < today]
        return max(past) if past else None

    def days_to_next_earnings(self, symbol: str) -> Optional[int]:
        """
        Return calendar days until the next earnings date.

        Args:
            symbol: Ticker string.

        Returns:
            Non-negative integer days, or None if unknown.
        """
        next_date = self.get_next_earnings(symbol)
        if next_date is None:
            return None
        return (next_date - date.today()).days

    def days_since_last_earnings(self, symbol: str) -> Optional[int]:
        """
        Return calendar days since the most recent earnings date.

        Args:
            symbol: Ticker string.

        Returns:
            Non-negative integer days, or None if unknown.
        """
        last_date = self.get_last_earnings(symbol)
        if last_date is None:
            return None
        return (date.today() - last_date).days

    def trading_days_to_next_earnings(self, symbol: str) -> Optional[int]:
        """
        Trading days until the next earnings date (weekends excluded).

        This is what exclusion-zone logic should use: the config windows are
        documented in trading days, so a Friday reporter is 1 trading day (not
        3 calendar days) from the following Monday. Holidays inside the window
        are not subtracted (rare for a ±1–3 day zone); weekends are.
        """
        next_date = self.get_next_earnings(symbol)
        if next_date is None:
            return None
        return _trading_days_between(date.today(), next_date)

    def trading_days_since_last_earnings(self, symbol: str) -> Optional[int]:
        """Trading days since the most recent earnings date (weekends excluded)."""
        last_date = self.get_last_earnings(symbol)
        if last_date is None:
            return None
        return _trading_days_between(last_date, date.today())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_all_dates(self, symbol: str) -> list[date]:
        """Merge yfinance and Alpha Vantage dates, return sorted unique list."""
        cache_key = f"earnings_dates_{symbol}"
        cached = self._cache.get(cache_key, ttl_seconds=_YFINANCE_TTL)
        if cached is not None:
            return [date.fromisoformat(d) for d in cached]

        dates: list[date] = []

        # Primary: yfinance
        yf_dates = self._yf.get_earnings_dates(symbol) or []
        dates.extend(yf_dates)

        # Secondary: Alpha Vantage bulk
        if self._av_key:
            av_dates = self._get_av_dates(symbol)
            if av_dates:
                # Take the more conservative (earlier) date where both sources have one
                dates.extend(av_dates)

        if not dates:
            return []

        unique = sorted(set(dates))
        self._cache.set(cache_key, [d.isoformat() for d in unique])
        return unique

    def _get_av_dates(self, symbol: str) -> list[date]:
        """Fetch upcoming earnings from Alpha Vantage bulk CSV (cached 24h)."""
        if not self._av_key:
            return []

        if self._av_bulk is None:
            self._av_bulk = self._load_av_bulk()

        return self._av_bulk.get(symbol.upper(), [])

    def _load_av_bulk(self) -> dict[str, list[date]]:
        """Download and parse Alpha Vantage EARNINGS_CALENDAR CSV."""
        cache_key = "av_earnings_calendar_bulk"
        cached = self._cache.get(cache_key, ttl_seconds=_ALPHA_VANTAGE_TTL)
        if cached is not None:
            result: dict[str, list[date]] = {}
            for sym, dates in cached.items():
                result[sym] = [date.fromisoformat(d) for d in dates]
            return result

        url = (
            "https://www.alphavantage.co/query"
            f"?function=EARNINGS_CALENDAR&horizon=3month&apikey={self._av_key}"
        )
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            reader = csv.DictReader(io.StringIO(resp.text))
            result = {}
            for row in reader:
                sym = row.get("symbol", "").upper()
                report_date_str = row.get("reportDate", "")
                if not sym or not report_date_str:
                    continue
                try:
                    d = date.fromisoformat(report_date_str)
                except ValueError:
                    continue
                result.setdefault(sym, []).append(d)

            # Cache the serialisable form
            self._cache.set(cache_key, {
                s: [d.isoformat() for d in dates]
                for s, dates in result.items()
            })
            logger.info("Alpha Vantage earnings calendar loaded (%d symbols)", len(result))
            return result
        except Exception as exc:
            logger.warning("Alpha Vantage earnings calendar failed: %s", exc)
            return {}
