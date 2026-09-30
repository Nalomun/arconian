"""
Tests for data/earnings_calendar.py trading-day distance logic (F-15).

The exclusion windows are documented in trading days, so the calendar must
report trading-day distances — a Friday reporter is 1 trading day, not 3
calendar days, from the following Monday.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, timedelta
from unittest.mock import MagicMock

from data.earnings_calendar import EarningsCalendar, _trading_days_between


class TestTradingDaysBetween:

    def test_friday_to_monday_is_one(self):
        # Fri 2025-01-17 -> Mon 2025-01-20: only Friday counts (weekend excluded)
        assert _trading_days_between(date(2025, 1, 17), date(2025, 1, 20)) == 1

    def test_same_day_is_zero(self):
        assert _trading_days_between(date(2025, 1, 15), date(2025, 1, 15)) == 0

    def test_full_business_week_is_five(self):
        assert _trading_days_between(date(2025, 1, 15), date(2025, 1, 22)) == 5

    def test_spans_weekend(self):
        # Fri -> next Fri = 5 trading days (one weekend skipped)
        assert _trading_days_between(date(2025, 1, 17), date(2025, 1, 24)) == 5


class TestTradingDayWrappers:

    def _cal(self, tmp_path) -> EarningsCalendar:
        return EarningsCalendar(yf_adapter=MagicMock(), cache_dir=str(tmp_path))

    def test_to_next_none_when_unknown(self, tmp_path):
        cal = self._cal(tmp_path)
        cal.get_next_earnings = MagicMock(return_value=None)
        assert cal.trading_days_to_next_earnings("X") is None

    def test_since_last_none_when_unknown(self, tmp_path):
        cal = self._cal(tmp_path)
        cal.get_last_earnings = MagicMock(return_value=None)
        assert cal.trading_days_since_last_earnings("X") is None

    def test_to_next_delegates_to_trading_day_count(self, tmp_path):
        cal = self._cal(tmp_path)
        future = date.today() + timedelta(days=14)
        cal.get_next_earnings = MagicMock(return_value=future)
        assert cal.trading_days_to_next_earnings("X") == _trading_days_between(date.today(), future)

    def test_since_last_delegates_to_trading_day_count(self, tmp_path):
        cal = self._cal(tmp_path)
        past = date.today() - timedelta(days=14)
        cal.get_last_earnings = MagicMock(return_value=past)
        assert cal.trading_days_since_last_earnings("X") == _trading_days_between(past, date.today())
