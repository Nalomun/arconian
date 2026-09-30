"""Tests for data/macro_calendar.py (F-16)."""

import logging
import sys
import os
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import data.macro_calendar as mc
from data.macro_calendar import is_macro_event_day, get_all_macro_events


class TestFomcDates:
    def test_june_fomc_is_17th_not_10th(self):
        """F-16: the June 2026 FOMC decision day is Jun 17 (Jun 16–17), not Jun 10."""
        assert is_macro_event_day(date(2026, 6, 17)) == "FOMC"
        assert is_macro_event_day(date(2026, 6, 10)) is None

    def test_other_known_fomc_dates_intact(self):
        assert is_macro_event_day(date(2026, 1, 28)) == "FOMC"
        assert is_macro_event_day(date(2026, 12, 9)) == "FOMC"

    def test_non_event_day_returns_none(self):
        assert is_macro_event_day(date(2026, 6, 18)) is None


class TestUnknownYearWarning:
    def test_unknown_year_warns_once(self, caplog):
        # Reset the module-level "already warned" set for a clean assertion.
        mc._warned_years.discard(2027)
        with caplog.at_level(logging.WARNING):
            assert is_macro_event_day(date(2027, 6, 17)) is None
            assert is_macro_event_day(date(2027, 9, 16)) is None
        warnings = [r for r in caplog.records if "no data for 2027" in r.getMessage()]
        # Warns about the missing year, but only once (deduped per year).
        assert len(warnings) == 1

    def test_known_year_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING):
            is_macro_event_day(date(2026, 6, 17))
        assert not [r for r in caplog.records if "no data" in r.getMessage()]


def test_get_all_macro_events_sorted_and_nonempty():
    events = get_all_macro_events()
    assert events
    dates = [d for d, _ in events]
    assert dates == sorted(dates)
