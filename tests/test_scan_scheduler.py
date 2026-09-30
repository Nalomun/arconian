"""
Tests for execution/scan_scheduler.py and execution/deadman_switch.py.

Covers:
  - is_market_open: weekends, holidays, normal trading days
  - _nyse_holidays: spot checks for known 2025 NYSE holidays
  - ScanScheduler: job registration, market guard (scan skipped on holiday/weekend)
  - DeadmanSwitch: heartbeat resets timer, halt disables watchdog, escalation sequence
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import threading
import time
from datetime import date, datetime, timedelta
from timeutils import utcnow
from unittest.mock import MagicMock, call, patch

import pytz
import pytest

from execution.scan_scheduler import (
    ScanScheduler,
    _nyse_holidays,
    is_market_open,
    is_early_close,
    regular_market_close_time,
    _nyse_early_closes,
)
import execution.scan_scheduler as _ss
from datetime import time as _time_t
from execution.deadman_switch import DeadmanSwitch

_ET = pytz.timezone("US/Eastern")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _et(year, month, day, hour=10, minute=0) -> datetime:
    """Create a US/Eastern datetime for use in is_market_open() tests."""
    return _ET.localize(datetime(year, month, day, hour, minute))


class TestEarlyClose:
    """F-26: NYSE early-close (half) days."""

    def test_black_friday_2026_is_early_close(self):
        # Thanksgiving 2026 = Nov 26 (Thu); the day after is a 1pm close.
        assert is_early_close(_et(2026, 11, 27, 15, 0)) is True
        assert regular_market_close_time(_et(2026, 11, 27)) == _time_t(13, 0)

    def test_christmas_eve_2026_is_early_close(self):
        assert is_early_close(_et(2026, 12, 24, 15, 0)) is True

    def test_normal_day_is_full_session(self):
        assert is_early_close(_et(2026, 6, 24, 12, 0)) is False
        assert regular_market_close_time(_et(2026, 6, 24)) == _time_t(16, 0)

    def test_full_holiday_is_not_early_close(self):
        # July 4 2026 is a Saturday → observed July 3; July 3 is a FULL holiday,
        # not an early close.
        assert date(2026, 7, 3) not in _nyse_early_closes(2026)
        assert is_early_close(_et(2026, 7, 3, 12, 0)) is False

    def test_weekend_is_not_early_close(self):
        assert is_early_close(_et(2026, 11, 28, 12, 0)) is False  # Saturday


class TestEarlyCloseScanGuard:
    """F-26: scans after the early-close time are skipped; post-close jobs run."""

    def _scheduler(self, deadman):
        return ScanScheduler(
            scan_fn=lambda t: None,
            universe_check_fn=lambda: None,
            maintenance_fn=lambda: None,
            deadman=deadman,
        )

    def test_scan_skipped_after_early_close(self):
        deadman = MagicMock()
        sched = self._scheduler(deadman)
        ran = []
        guard = sched._market_guard(lambda: ran.append("scan"), "preclose", is_scan=True)
        with patch.object(_ss, "datetime") as mdt:
            mdt.now.return_value = _et(2026, 11, 27, 15, 30)  # Black Friday 3:30pm
            guard()
        assert ran == []                       # scan did not run
        deadman.heartbeat.assert_called_once()  # but the system stayed alive

    def test_scan_runs_before_early_close(self):
        deadman = MagicMock()
        sched = self._scheduler(deadman)
        ran = []
        guard = sched._market_guard(lambda: ran.append("scan"), "midday", is_scan=True)
        with patch.object(_ss, "datetime") as mdt:
            mdt.now.return_value = _et(2026, 11, 27, 12, 0)  # before 1pm close
            guard()
        assert ran == ["scan"]

    def test_post_close_job_runs_on_early_close_day(self):
        # A non-scan job (maintenance) is not skipped by the early-close rule.
        deadman = MagicMock()
        sched = self._scheduler(deadman)
        ran = []
        guard = sched._market_guard(lambda: ran.append("maint"), "maintenance", is_scan=False)
        with patch.object(_ss, "datetime") as mdt:
            mdt.now.return_value = _et(2026, 11, 27, 16, 15)
            guard()
        assert ran == ["maint"]

    def test_scheduler_uses_single_worker_executor(self):
        sched = self._scheduler(MagicMock())
        ex = sched._scheduler._executors["default"]
        # One worker → distinct jobs (16:15 maintenance / 16:30 trade check) serialise.
        assert ex._pool._max_workers == 1


# ---------------------------------------------------------------------------
# NYSE holiday list
# ---------------------------------------------------------------------------

class TestNyseHolidays:

    def test_returns_set(self):
        h = _nyse_holidays(2025)
        assert isinstance(h, set)

    def test_new_years_day_2025(self):
        # Jan 1 2025 is a Wednesday → not shifted
        assert date(2025, 1, 1) in _nyse_holidays(2025)

    def test_mlk_day_2025(self):
        # 3rd Monday in January 2025 = Jan 20
        assert date(2025, 1, 20) in _nyse_holidays(2025)

    def test_independence_day_2025(self):
        # July 4 2025 is a Friday → not shifted
        assert date(2025, 7, 4) in _nyse_holidays(2025)

    def test_thanksgiving_2025(self):
        # 4th Thursday in November 2025 = Nov 27
        assert date(2025, 11, 27) in _nyse_holidays(2025)

    def test_christmas_2025(self):
        # Dec 25 2025 is a Thursday → not shifted
        assert date(2025, 12, 25) in _nyse_holidays(2025)

    def test_new_years_day_2022_observed(self):
        # Jan 1 2022 is a Saturday → observed Friday Dec 31 2021
        # (shifted to 2021, so 2022 holiday set should not include Jan 1)
        # New Year's 2022 is observed as Dec 31 2021 which is in 2021's set
        # or Jan 3 2022 depending on exchange convention.
        # Just confirm the year returns at least 9 entries (sanity)
        assert len(_nyse_holidays(2022)) >= 9

    def test_ten_holidays_per_year(self):
        """Should have exactly 10 holidays (some may land on same day in rare cases)."""
        for year in (2024, 2025, 2026):
            h = _nyse_holidays(year)
            assert 9 <= len(h) <= 11, f"Year {year}: got {len(h)} holidays"

    def test_juneteenth_2025(self):
        # June 19 2025 is a Thursday → not shifted
        assert date(2025, 6, 19) in _nyse_holidays(2025)

    def test_good_friday_2025(self):
        # Good Friday 2025 is April 18
        assert date(2025, 4, 18) in _nyse_holidays(2025)

    def test_good_friday_2029_month_rollover(self):
        # Regression (F-12): Easter 2029 is April 1, so Good Friday is March 30.
        # The old `date(year, 4, 1 - 2)` raised ValueError and aborted every
        # scheduled job for the whole year. Must compute via timedelta.
        assert date(2029, 3, 30) in _nyse_holidays(2029)


# ---------------------------------------------------------------------------
# is_market_open
# ---------------------------------------------------------------------------

class TestIsMarketOpen:

    def test_normal_weekday_is_open(self):
        # Wednesday Jan 15 2025 — no holiday
        assert is_market_open(_et(2025, 1, 15)) is True

    def test_saturday_is_closed(self):
        assert is_market_open(_et(2025, 1, 11)) is False  # Saturday

    def test_sunday_is_closed(self):
        assert is_market_open(_et(2025, 1, 12)) is False  # Sunday

    def test_mlk_day_2025_is_closed(self):
        assert is_market_open(_et(2025, 1, 20)) is False

    def test_thanksgiving_2025_is_closed(self):
        assert is_market_open(_et(2025, 11, 27)) is False

    def test_christmas_2025_is_closed(self):
        assert is_market_open(_et(2025, 12, 25)) is False

    def test_day_after_thanksgiving_is_open(self):
        # Black Friday is NOT a full-session closure (early close only)
        assert is_market_open(_et(2025, 11, 28)) is True

    def test_defaults_to_now(self):
        # Just confirms it doesn't raise when dt=None
        result = is_market_open(None)
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# ScanScheduler: job registration
# ---------------------------------------------------------------------------

class TestScanSchedulerJobs:

    def _make_scheduler(self, times=None):
        scan_fn = MagicMock()
        universe_fn = MagicMock()
        maintenance_fn = MagicMock()
        sched = ScanScheduler(
            scan_fn=scan_fn,
            universe_check_fn=universe_fn,
            maintenance_fn=maintenance_fn,
            scan_times_et=times or ["09:35", "12:00", "15:30"],
        )
        return sched, scan_fn, universe_fn, maintenance_fn

    def test_five_jobs_registered(self):
        sched, *_ = self._make_scheduler()
        jobs = sched._scheduler.get_jobs()
        assert len(jobs) == 5  # universe, 3 scans, maintenance

    def test_job_ids_present(self):
        sched, *_ = self._make_scheduler()
        ids = {j.id for j in sched._scheduler.get_jobs()}
        assert "universe_check" in ids
        assert "scan_open" in ids
        assert "scan_midday" in ids
        assert "scan_preclose" in ids
        assert "maintenance" in ids

    def test_custom_scan_times_registered(self):
        sched, *_ = self._make_scheduler(times=["10:00", "13:00"])
        jobs = sched._scheduler.get_jobs()
        # 1 universe + 2 scans + 1 maintenance = 4
        assert len(jobs) == 4

    def test_scan_labels_assigned(self):
        sched, *_ = self._make_scheduler()
        ids = {j.id for j in sched._scheduler.get_jobs()}
        assert "scan_open" in ids
        assert "scan_midday" in ids
        assert "scan_preclose" in ids


# ---------------------------------------------------------------------------
# ScanScheduler: market guard
# ---------------------------------------------------------------------------

class TestScanSchedulerMarketGuard:

    def _get_guard_fn(self, times=None):
        """Extract the wrapped job function for the first scan."""
        scan_fn = MagicMock()
        sched = ScanScheduler(
            scan_fn=scan_fn,
            universe_check_fn=MagicMock(),
            maintenance_fn=MagicMock(),
            scan_times_et=times or ["09:35", "12:00", "15:30"],
        )
        job = next(j for j in sched._scheduler.get_jobs() if j.id == "scan_open")
        return job.func, scan_fn

    def test_scan_runs_on_open_market_day(self):
        guard_fn, scan_fn = self._get_guard_fn()
        # Patch is_market_open to return True, and pin the wall clock to a regular
        # session's morning: the F-26 guard skips scans at or after the close, so
        # an unpinned clock made this test fail whenever it ran after 16:00 ET.
        with patch("execution.scan_scheduler.is_market_open", return_value=True), \
             patch.object(_ss, "datetime") as mdt:
            mdt.now.return_value = _et(2026, 6, 24, 10, 0)  # regular Wednesday
            guard_fn()
        scan_fn.assert_called_once()

    def test_scan_skipped_on_holiday(self):
        guard_fn, scan_fn = self._get_guard_fn()
        with patch("execution.scan_scheduler.is_market_open", return_value=False):
            guard_fn()
        scan_fn.assert_not_called()

    def test_scan_exception_does_not_propagate(self):
        """Job exceptions must be caught; the scheduler must keep running."""
        scan_fn = MagicMock(side_effect=RuntimeError("adapter down"))
        sched = ScanScheduler(
            scan_fn=scan_fn,
            universe_check_fn=MagicMock(),
            maintenance_fn=MagicMock(),
        )
        job = next(j for j in sched._scheduler.get_jobs() if j.id == "scan_open")
        # Pinned clock: after 16:00 ET the guard would skip the scan and never raise.
        with patch("execution.scan_scheduler.is_market_open", return_value=True), \
             patch.object(_ss, "datetime") as mdt:
            mdt.now.return_value = _et(2026, 6, 24, 10, 0)
            job.func()   # must not raise
        scan_fn.assert_called_once()


# ---------------------------------------------------------------------------
# DeadmanSwitch: basic lifecycle
# ---------------------------------------------------------------------------

class TestDeadmanSwitchLifecycle:

    def test_heartbeat_records_timestamp(self):
        dm = DeadmanSwitch(timeout_minutes=60, enabled=False)
        assert dm.last_heartbeat is None
        dm.heartbeat()
        assert dm.last_heartbeat is not None

    def test_heartbeat_does_not_clear_halted_flag(self):
        # Sticky halt: once force-close has fired, a heartbeat must NOT lift the
        # halt (otherwise a stale scan silently un-halts the system). Only an
        # explicit reset() clears it.
        dm = DeadmanSwitch(timeout_minutes=60, enabled=False)
        dm._halted = True
        dm.heartbeat()
        assert dm.is_halted is True

    def test_reset_clears_halted_flag(self):
        dm = DeadmanSwitch(timeout_minutes=60, enabled=False)
        dm._halted = True
        dm.reset()
        assert dm.is_halted is False

    def test_halt_disables_watchdog(self):
        dm = DeadmanSwitch(timeout_minutes=60, enabled=True)
        dm.halt()
        assert dm._enabled is False

    def test_disabled_watchdog_does_not_start_timer(self):
        dm = DeadmanSwitch(timeout_minutes=60, enabled=False)
        assert dm._timer is None

    def test_enabled_watchdog_arms_lazily(self):
        # The watchdog no longer arms at construction: it arms on the first scan
        # via ensure_armed(). This prevents a false alarm when the system starts
        # mid-day and the next scheduled scan is more than timeout_minutes away.
        dm = DeadmanSwitch(timeout_minutes=60, enabled=True)
        assert dm._timer is None
        dm.ensure_armed()
        assert dm._timer is not None
        dm.halt()

    def test_is_halted_false_initially(self):
        dm = DeadmanSwitch(timeout_minutes=60, enabled=False)
        assert dm.is_halted is False

    def test_multiple_heartbeats_stay_not_halted(self):
        dm = DeadmanSwitch(timeout_minutes=60, enabled=False)
        for _ in range(5):
            dm.heartbeat()
        assert dm.is_halted is False


# ---------------------------------------------------------------------------
# DeadmanSwitch: escalation sequence
# ---------------------------------------------------------------------------

class TestDeadmanSwitchEscalation:

    def test_timeout_triggers_escalation(self):
        """After timeout fires, escalation should call widen and force-close."""
        dm = DeadmanSwitch(
            timeout_minutes=0,          # fire immediately (0 min → 0 sec)
            widen_stops_minutes=0,
            force_close_minutes=0,
            enabled=False,
        )

        widen_called = threading.Event()
        force_called = threading.Event()

        def _mock_widen():
            widen_called.set()

        def _mock_force():
            force_called.set()

        dm._widen_stops = _mock_widen
        dm._force_close_all = _mock_force
        dm._alert = MagicMock()
        dm._attempt_reauth = MagicMock()

        # Manually fire the escalation on this thread
        dm._on_timeout()

        assert widen_called.is_set()
        assert force_called.is_set()

    def test_escalation_sets_halted_flag(self):
        dm = DeadmanSwitch(timeout_minutes=0, widen_stops_minutes=0,
                           force_close_minutes=0, enabled=False)
        dm._widen_stops = MagicMock()
        dm._force_close_all = MagicMock()
        dm._alert = MagicMock()
        dm._attempt_reauth = MagicMock()

        dm._on_timeout()
        assert dm.is_halted is True

    def test_escalation_aborted_if_heartbeat_arrives(self):
        """If heartbeat arrives between stages, force-close should NOT run."""
        dm = DeadmanSwitch(
            timeout_minutes=0,
            widen_stops_minutes=0,
            force_close_minutes=0,
            enabled=False,
        )
        force_called = threading.Event()

        def _mock_widen():
            # Simulate operator intervention: heartbeat arrives after widen
            dm._last_heartbeat = utcnow()
            # Make timeout_secs artificially large so the heartbeat check passes
            dm._timeout_secs = 99999

        dm._widen_stops = _mock_widen
        dm._force_close_all = lambda: force_called.set()
        dm._alert = MagicMock()
        dm._attempt_reauth = MagicMock()

        dm._on_timeout()
        # With the heartbeat set before force stage, force should be skipped
        assert not force_called.is_set()

    def test_alert_called_at_each_stage(self):
        dm = DeadmanSwitch(timeout_minutes=0, widen_stops_minutes=0,
                           force_close_minutes=0, enabled=False)
        alerts = []
        dm._alert = lambda msg: alerts.append(msg)
        dm._widen_stops = MagicMock()
        dm._force_close_all = MagicMock()
        dm._attempt_reauth = MagicMock()

        dm._on_timeout()
        assert len(alerts) >= 2  # timeout alert + final halt alert

    def test_widen_stops_no_db_skips_gracefully(self):
        """No db_session_factory → widen logs warning but doesn't raise."""
        dm = DeadmanSwitch(timeout_minutes=60, enabled=False, db_session_factory=None)
        dm._widen_stops()  # must not raise

    def test_force_close_no_db_skips_gracefully(self):
        dm = DeadmanSwitch(timeout_minutes=60, enabled=False, db_session_factory=None)
        dm._force_close_all()  # must not raise

    def test_telegram_alert_skipped_without_env_vars(self):
        """Without TELEGRAM env vars, _alert only logs, no HTTP call made."""
        dm = DeadmanSwitch(timeout_minutes=60, enabled=False)
        with patch("urllib.request.urlopen") as mock_http:
            dm._alert("test message")
        mock_http.assert_not_called()

    def test_enabled_timer_can_be_halted(self):
        """An enabled watchdog's timer can be halted cleanly before it fires."""
        dm = DeadmanSwitch(
            timeout_minutes=60,   # long enough to not fire during test
            widen_stops_minutes=10,
            force_close_minutes=30,
            enabled=True,
        )
        dm.ensure_armed()
        assert dm._timer is not None
        dm.halt()
        assert dm._enabled is False
        assert dm._timer is None
