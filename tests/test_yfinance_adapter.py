"""Tests for data/yfinance_adapter.py — sector mapping and caching logic."""

from unittest.mock import MagicMock, patch
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.yfinance_adapter import (
    YFinanceAdapter,
    TickerInfo,
    normalize_sector,
    get_sector_etf,
    GICS_SECTOR_ETF_MAP,
    YFINANCE_SECTOR_NORMALIZE,
    SECTOR_ETFS,
)


# ---------------------------------------------------------------------------
# Sector mapping (pure logic — no network)
# ---------------------------------------------------------------------------

class TestSectorMapping:
    def test_all_yfinance_sectors_map_to_gics(self):
        for yf_sector, gics in YFINANCE_SECTOR_NORMALIZE.items():
            assert gics in GICS_SECTOR_ETF_MAP, (
                f"yfinance sector {yf_sector!r} maps to GICS {gics!r} "
                f"but that GICS sector has no ETF"
            )

    def test_all_gics_sectors_have_etf(self):
        for sector, etf in GICS_SECTOR_ETF_MAP.items():
            assert etf in SECTOR_ETFS, f"{sector} → {etf} not in SECTOR_ETFS list"

    def test_sector_etfs_list_completeness(self):
        assert len(SECTOR_ETFS) == 11

    def test_normalize_technology_to_gics(self):
        assert normalize_sector("Technology") == "Information Technology"

    def test_normalize_healthcare_to_gics(self):
        assert normalize_sector("Healthcare") == "Health Care"

    def test_normalize_returns_none_for_unknown(self):
        assert normalize_sector("Unknown Sector") is None

    def test_normalize_none_input(self):
        assert normalize_sector(None) is None

    def test_get_sector_etf_xlk(self):
        assert get_sector_etf("Technology") == "XLK"

    def test_get_sector_etf_xlv(self):
        assert get_sector_etf("Healthcare") == "XLV"

    def test_get_sector_etf_none_for_unknown(self):
        assert get_sector_etf("Alien Technology") is None

    def test_all_roundtrips_work(self):
        """Every yfinance sector should produce a valid ETF ticker."""
        for yf_sector in YFINANCE_SECTOR_NORMALIZE:
            etf = get_sector_etf(yf_sector)
            assert etf is not None, f"No ETF for yfinance sector {yf_sector!r}"


# ---------------------------------------------------------------------------
# TickerInfo construction
# ---------------------------------------------------------------------------

class TestTickerInfo:
    def _make_adapter(self, tmp_path) -> YFinanceAdapter:
        with patch("yfinance.download"), patch("yfinance.Ticker"):
            adapter = YFinanceAdapter(cache_dir=str(tmp_path / ".cache"))
        return adapter

    def test_get_ticker_info_returns_dataclass(self, tmp_path):
        adapter = self._make_adapter(tmp_path)
        mock_info = {
            "symbol": "SOFI",
            "sector": "Financial Services",
            "industry": "Credit Services",
            "marketCap": 13_000_000_000,
            "shortPercentOfFloat": 0.08,
            "sharesOutstanding": 1_000_000_000,
        }
        with patch.object(adapter._yf, "Ticker") as mock_ticker:
            mock_ticker.return_value.info = mock_info
            result = adapter.get_ticker_info("SOFI")

        assert isinstance(result, TickerInfo)
        assert result.ticker == "SOFI"
        assert result.sector_yfinance == "Financial Services"
        assert result.sector_gics == "Financials"
        assert result.sector_etf == "XLF"
        assert result.market_cap_mm == pytest.approx(13_000.0)
        assert result.short_pct_float == 0.08

    def test_get_ticker_info_returns_none_on_empty_response(self, tmp_path):
        adapter = self._make_adapter(tmp_path)
        with patch.object(adapter._yf, "Ticker") as mock_ticker:
            mock_ticker.return_value.info = {}
            result = adapter.get_ticker_info("BAD")
        assert result is None

    def test_get_ticker_info_returns_none_on_exception(self, tmp_path):
        adapter = self._make_adapter(tmp_path)
        with patch.object(adapter._yf, "Ticker") as mock_ticker:
            mock_ticker.side_effect = Exception("network error")
            result = adapter.get_ticker_info("ERR")
        assert result is None

    def test_get_ticker_info_caches_result(self, tmp_path):
        adapter = self._make_adapter(tmp_path)
        mock_info = {
            "symbol": "AAPL",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "marketCap": 3_000_000_000_000,
            "shortPercentOfFloat": 0.005,
            "sharesOutstanding": 15_000_000_000,
        }
        with patch.object(adapter._yf, "Ticker") as mock_ticker:
            mock_ticker.return_value.info = mock_info
            adapter.get_ticker_info("AAPL")
            adapter.get_ticker_info("AAPL")
            # yfinance.Ticker should only be called once due to caching
            assert mock_ticker.call_count == 1


# ---------------------------------------------------------------------------
# Macro calendar (no network)
# ---------------------------------------------------------------------------

from data.macro_calendar import is_macro_event_day, get_all_macro_events
from datetime import date

class TestMacroCalendar:
    def test_fomc_date_detected(self):
        assert is_macro_event_day(date(2026, 1, 28)) == "FOMC"

    def test_cpi_date_detected(self):
        assert is_macro_event_day(date(2026, 1, 14)) == "CPI"

    def test_nfp_date_detected(self):
        assert is_macro_event_day(date(2026, 1, 9)) == "NFP"

    def test_non_event_day_returns_none(self):
        assert is_macro_event_day(date(2026, 1, 2)) is None

    def test_get_all_events_sorted(self):
        events = get_all_macro_events()
        dates = [e[0] for e in events]
        assert dates == sorted(dates)

    def test_all_events_have_valid_type(self):
        for _, event_type in get_all_macro_events():
            assert event_type in {"FOMC", "CPI", "NFP"}

    def test_accepts_datetime_input(self):
        from datetime import datetime
        dt = datetime(2026, 1, 28, 14, 30)
        assert is_macro_event_day(dt) == "FOMC"
