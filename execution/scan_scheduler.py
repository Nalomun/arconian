"""
APScheduler-based scan orchestrator.

Schedules the three daily scans and after-close maintenance tasks defined
in the whitepaper. All times are US/Eastern. Holidays and weekends are
skipped via a market-hours guard that wraps every job.

Job schedule (times from config.execution.scan_times_et):
  Default: 09:35, 12:00, 15:30 ET — signal scans
  08:50 ET  — universe daily filter check (pre-market)
  16:15 ET  — after-close maintenance (outcome collection, IC update, cost validation)

Design notes:
  - APScheduler 3.x CronTrigger in US/Eastern handles DST automatically.
  - Each job is wrapped by _market_hours_guard(); if the market is closed
    (weekend or NYSE holiday) the scan body is skipped and a debug log is
    written. Holidays are detected by checking if today is in a pre-computed
    annual NYSE holiday list (federal + early-close dates are not included
    in the skip list — early-close markets still run scans).
  - The scheduler runs in blocking mode (scheduler.start()). Signals SIGINT
    and SIGTERM are handled by main.py to call scheduler.shutdown(wait=False).
  - The deadman switch is poked after every successful scan completion via
    DeadmanSwitch.heartbeat().

Whitepaper reference: Section 5.1 (Scan Architecture)
"""

import logging
import signal as _signal
from datetime import date, datetime, time as _time, timedelta
from typing import Callable, Optional

import pytz
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_ET = pytz.timezone("US/Eastern")

# Regular and early (half-day) NYSE close times, ET.
_REGULAR_CLOSE = _time(16, 0)
_EARLY_CLOSE = _time(13, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the nth occurrence of weekday (0=Mon) in the given month."""
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return date(year, month, 1 + delta + (n - 1) * 7)

# ---------------------------------------------------------------------------
# NYSE holiday list (year-independent; regenerate annually)
# Only full-session closures are listed. Early closes are not skipped.
# ---------------------------------------------------------------------------

def _nyse_holidays(year: int) -> set[date]:
    """
    Return the set of NYSE full-session holiday dates for the given year.

    Uses a fixed-rule approximation suitable for Phase 0/1:
      New Year's Day, MLK Day (3rd Mon Jan), Presidents' Day (3rd Mon Feb),
      Good Friday (variable), Memorial Day (last Mon May),
      Juneteenth (Jun 19, observed), Independence Day (Jul 4, observed),
      Labor Day (1st Mon Sep), Thanksgiving (4th Thu Nov), Christmas (Dec 25).

    For production use, replace with a data provider (e.g. pandas_market_calendars).
    """
    import calendar

    def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
        """Return the nth occurrence of weekday (0=Mon) in the given month."""
        first = date(year, month, 1)
        delta = (weekday - first.weekday()) % 7
        return date(year, month, 1 + delta + (n - 1) * 7)

    def last_weekday(year: int, month: int, weekday: int) -> date:
        last_day = calendar.monthrange(year, month)[1]
        last = date(year, month, last_day)
        delta = (last.weekday() - weekday) % 7
        return date(year, month, last_day - delta)

    def observed(d: date) -> date:
        """Shift weekend holidays to the nearest weekday."""
        if d.weekday() == 5:  # Saturday → Friday
            return date(d.year, d.month, d.day - 1) if d.day > 1 else d
        if d.weekday() == 6:  # Sunday → Monday
            return date(d.year, d.month, d.day + 1)
        return d

    def good_friday(year: int) -> date:
        """Compute Good Friday (2 days before Easter) via the Anonymous Gregorian algorithm."""
        a = year % 19
        b, c = divmod(year, 100)
        d, e = divmod(b, 4)
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = divmod(c, 4)
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month = (h + l - 7 * m + 114) // 31
        day = ((h + l - 7 * m + 114) % 31) + 1
        easter = date(year, month, day)
        # Subtract via timedelta so the result rolls over month boundaries.
        # date(year, easter.month, easter.day - 2) raises ValueError when Easter
        # falls on Apr 1 or 2 (e.g. 2029 → Apr 1 → day -1). Good Friday is always
        # 2 days before Easter Sunday.
        return easter - timedelta(days=2)

    holidays = {
        observed(date(year, 1, 1)),                       # New Year's Day
        nth_weekday(year, 1, 0, 3),                       # MLK Day
        nth_weekday(year, 2, 0, 3),                       # Presidents' Day
        good_friday(year),                                 # Good Friday
        last_weekday(year, 5, 0),                         # Memorial Day
        observed(date(year, 6, 19)),                      # Juneteenth
        observed(date(year, 7, 4)),                       # Independence Day
        nth_weekday(year, 9, 0, 1),                       # Labor Day
        nth_weekday(year, 11, 3, 4),                      # Thanksgiving
        observed(date(year, 12, 25)),                     # Christmas
    }
    return holidays


def _nyse_early_closes(year: int) -> set[date]:
    """
    Return NYSE early-close (1:00 PM ET) dates for the year.

    Fixed-rule approximation (matches the full-holiday list's spirit; replace
    with a data provider for production):
      - Day after Thanksgiving (always a 1pm close).
      - Christmas Eve (Dec 24) when it's a weekday and not the observed
        Christmas full holiday.
      - July 3 when it's a weekday and not the observed Independence Day holiday.
    """
    holidays = _nyse_holidays(year)
    early: set[date] = set()

    # Day after Thanksgiving (Thanksgiving = 4th Thursday of November).
    early.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))

    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5 and dec24 not in holidays:
        early.add(dec24)

    jul3 = date(year, 7, 3)
    if jul3.weekday() < 5 and jul3 not in holidays:
        early.add(jul3)

    return early


_HOLIDAY_CACHE: dict[int, set[date]] = {}
_EARLY_CLOSE_CACHE: dict[int, set[date]] = {}


def is_early_close(dt: Optional[datetime] = None) -> bool:
    """True if the given date is an NYSE early-close (half) day."""
    if dt is None:
        dt = datetime.now(_ET)
    today = dt.date()
    if not is_market_open(dt):
        return False
    year = today.year
    if year not in _EARLY_CLOSE_CACHE:
        _EARLY_CLOSE_CACHE[year] = _nyse_early_closes(year)
    return today in _EARLY_CLOSE_CACHE[year]


def regular_market_close_time(dt: Optional[datetime] = None) -> _time:
    """Return the NYSE close time (ET) for the given date: 13:00 on early-close
    days, 16:00 otherwise."""
    return _EARLY_CLOSE if is_early_close(dt) else _REGULAR_CLOSE


def is_market_open(dt: Optional[datetime] = None) -> bool:
    """
    Return True if the NYSE is open for a full session on the given date.

    Args:
        dt: Datetime to check (US/Eastern). Defaults to now in US/Eastern.

    Returns:
        False on weekends and NYSE full-session holidays, True otherwise.
    """
    if dt is None:
        dt = datetime.now(_ET)
    today = dt.date()
    if today.weekday() >= 5:  # Saturday or Sunday
        return False
    year = today.year
    if year not in _HOLIDAY_CACHE:
        _HOLIDAY_CACHE[year] = _nyse_holidays(year)
    return today not in _HOLIDAY_CACHE[year]


# ---------------------------------------------------------------------------
# Scheduler builder
# ---------------------------------------------------------------------------

class ScanScheduler:
    """
    Wraps APScheduler to run signal scans and maintenance jobs.

    Args:
        scan_fn: Callable(scan_type: str) called for each signal scan.
            scan_type is 'morning', 'midday', or 'afternoon'.
        universe_check_fn: Callable() called pre-market for the daily
            universe filter check.
        maintenance_fn: Callable() called after close for outcome collection,
            IC update, and cost validation.
        deadman: Optional DeadmanSwitch instance; if provided, heartbeat()
            is called after each successful scan.
        scan_times_et: List of "HH:MM" strings for scan times (ET).
            Defaults to ["09:35", "12:00", "15:30"].
        timezone: pytz timezone string (default "US/Eastern").
    """

    _SCAN_LABELS = ("open", "midday", "preclose")   # must match signal_log scan_type constraint

    def __init__(
        self,
        scan_fn: Callable[[str], None],
        universe_check_fn: Callable[[], None],
        maintenance_fn: Callable[[], None],
        deadman=None,
        scan_times_et: Optional[list[str]] = None,
        timezone: str = "US/Eastern",
        trade_check_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        self._scan_fn = scan_fn
        self._universe_check_fn = universe_check_fn
        self._maintenance_fn = maintenance_fn
        self._trade_check_fn = trade_check_fn
        self._deadman = deadman
        self._tz = pytz.timezone(timezone)
        self._times = scan_times_et or ["09:35", "12:00", "15:30"]
        # Single-worker executor (F-26): the default thread pool runs distinct
        # jobs concurrently, so a long 16:15 maintenance could still be running
        # when the 16:30 trade check fired — racing on the same DB. One worker
        # serialises all jobs (they're spaced hours apart, so no throughput cost)
        # and max_instances=1 prevents a job overlapping itself.
        self._scheduler = BlockingScheduler(
            timezone=self._tz,
            executors={"default": ThreadPoolExecutor(max_workers=1)},
            job_defaults={"max_instances": 1},
        )
        self._running = False
        self._setup_jobs()

    def _market_guard(self, fn: Callable, label: str, is_scan: bool = False) -> Callable:
        """Wrap a job body: skip with a debug log if the market is closed."""
        def _wrapped():
            now = datetime.now(self._tz)
            if not is_market_open(now):
                logger.debug("_market_guard: %s skipped — market closed (%s)", label, now.date())
                # Market is closed but the system is alive — keep deadman satisfied
                if self._deadman is not None:
                    self._deadman.heartbeat()
                return
            # On an early-close (half) day, a scan scheduled after the 1pm close
            # would score (and auto-trade) against stale post-close prices (F-26).
            # Skip it; post-close jobs (maintenance/trade_check) still run.
            if is_scan and now.time() >= regular_market_close_time(now):
                logger.info(
                    "_market_guard: %s skipped — after early close (%s %s ET)",
                    label, now.date(), now.strftime("%H:%M"),
                )
                if self._deadman is not None:
                    self._deadman.heartbeat()
                return
            # Market is open — arm the deadman on the first scan of the session
            if self._deadman is not None:
                self._deadman.ensure_armed()
            try:
                fn()
            except Exception:
                logger.exception("Job %s raised an unhandled exception", label)
        return _wrapped

    def _make_scan_job(self, scan_type: str) -> Callable:
        deadman = self._deadman
        scan_fn = self._scan_fn

        def _run():
            logger.info("scan[%s] starting", scan_type)
            scan_fn(scan_type)
            logger.info("scan[%s] complete", scan_type)
            if deadman is not None:
                deadman.heartbeat()

        return _run

    def _setup_jobs(self) -> None:
        tz = self._tz

        # Misfire grace: APScheduler's default is 1s, which silently drops any
        # job whose scheduled time was missed (e.g. laptop asleep through the
        # tick). Cron jobs need a wider window. Coalesce=True collapses
        # multiple accumulated fires (e.g. machine off for days) into one.
        SCAN_GRACE = 600       # 10 min — scans tolerate small wake delays
        UNIVERSE_GRACE = 1800  # 30 min — pre-market data refresh
        BATCH_GRACE = 7200     # 2 hours — post-close batches must run if the
                               # day is to be processed at all

        # Universe pre-market check at 08:50 ET
        self._scheduler.add_job(
            self._market_guard(self._universe_check_fn, "universe_check"),
            CronTrigger(hour=8, minute=50, timezone=tz),
            id="universe_check",
            name="Universe daily filter check",
            replace_existing=True,
            misfire_grace_time=UNIVERSE_GRACE,
            coalesce=True,
        )

        # Three signal scans at configured times
        labels = self._SCAN_LABELS
        for idx, time_str in enumerate(self._times):
            h, m = (int(x) for x in time_str.split(":"))
            label = labels[idx] if idx < len(labels) else f"scan_{idx}"
            self._scheduler.add_job(
                self._market_guard(self._make_scan_job(label), label, is_scan=True),
                CronTrigger(hour=h, minute=m, timezone=tz),
                id=f"scan_{label}",
                name=f"Signal scan ({label}) {time_str} ET",
                replace_existing=True,
                misfire_grace_time=SCAN_GRACE,
                coalesce=True,
            )

        # After-close maintenance at 16:15 ET
        self._scheduler.add_job(
            self._market_guard(self._maintenance_fn, "maintenance"),
            CronTrigger(hour=16, minute=15, timezone=tz),
            id="maintenance",
            name="After-close maintenance",
            replace_existing=True,
            misfire_grace_time=BATCH_GRACE,
            coalesce=True,
        )

        # Paper trade state check at 16:30 ET (after maintenance, per Phase 1)
        if self._trade_check_fn is not None:
            self._scheduler.add_job(
                self._market_guard(self._trade_check_fn, "trade_check"),
                CronTrigger(hour=16, minute=30, timezone=tz),
                id="trade_check",
                name="Paper trade state check (4:30 PM ET)",
                replace_existing=True,
                misfire_grace_time=BATCH_GRACE,
                coalesce=True,
            )

    def start(self) -> None:
        """
        Start the scheduler in blocking mode.

        Registers SIGINT/SIGTERM handlers to shut down cleanly.
        Blocks until shutdown() is called or a signal is received.
        """
        self._running = True
        logger.info(
            "ScanScheduler starting — scans at %s ET",
            ", ".join(self._times),
        )

        def _shutdown(signum, frame):
            logger.info("ScanScheduler: received signal %s → shutting down", signum)
            self.shutdown()

        try:
            _signal.signal(_signal.SIGINT, _shutdown)
            _signal.signal(_signal.SIGTERM, _shutdown)
        except (OSError, ValueError):
            pass  # signal handling may not be available in all contexts

        self._scheduler.start()

    def shutdown(self, wait: bool = False) -> None:
        """Gracefully stop the scheduler."""
        if self._running:
            self._running = False
            self._scheduler.shutdown(wait=wait)
            logger.info("ScanScheduler: shutdown complete")
