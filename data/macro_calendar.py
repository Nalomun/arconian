"""
Macro calendar — FOMC, CPI, and NFP release dates.

Static data for 2026. Refresh each January by updating the date lists
below from the Federal Reserve and BLS publication schedules.

Federal Reserve FOMC schedule: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
BLS release schedule:           https://www.bls.gov/schedule/news_release/cpi.htm
                                https://www.bls.gov/schedule/news_release/empsit.htm

Whitepaper reference: Section 2.4 (Event Filtering)
Supplements reference: Doc 1, Section 1.8
"""

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 2026 FOMC meeting dates (decision day = second day of two-day meeting)
# ---------------------------------------------------------------------------
_FOMC_2026: list[date] = [
    date(2026, 1, 28),   # Jan 27–28
    date(2026, 3, 18),   # Mar 17–18
    date(2026, 4, 29),   # Apr 28–29  (with SEP)
    date(2026, 6, 17),   # Jun 16–17  (with SEP) — was wrongly Jun 9–10 (F-16)
    date(2026, 7, 29),   # Jul 28–29
    date(2026, 9, 16),   # Sep 15–16  (with SEP)
    date(2026, 10, 28),  # Oct 27–28
    date(2026, 12, 9),   # Dec 8–9    (with SEP)
]

# ---------------------------------------------------------------------------
# 2026 CPI release dates (BLS Consumer Price Index)
# ---------------------------------------------------------------------------
_CPI_2026: list[date] = [
    date(2026, 1, 14),
    date(2026, 2, 11),
    date(2026, 3, 11),
    date(2026, 4, 10),
    date(2026, 5, 13),
    date(2026, 6, 11),
    date(2026, 7, 14),
    date(2026, 8, 11),
    date(2026, 9, 10),
    date(2026, 10, 13),
    date(2026, 11, 12),
    date(2026, 12, 10),
]

# ---------------------------------------------------------------------------
# 2026 NFP (Non-Farm Payrolls / Employment Situation) release dates
# First Friday of each month, 8:30 AM ET
# ---------------------------------------------------------------------------
_NFP_2026: list[date] = [
    date(2026, 1, 9),
    date(2026, 2, 6),
    date(2026, 3, 6),
    date(2026, 4, 3),
    date(2026, 5, 1),
    date(2026, 6, 5),
    date(2026, 7, 10),  # shifted — July 4 holiday
    date(2026, 8, 7),
    date(2026, 9, 4),
    date(2026, 10, 2),
    date(2026, 11, 6),
    date(2026, 12, 4),
]

# Internal lookup: date → event name
_EVENT_MAP: dict[date, str] = {}
for _d in _FOMC_2026:
    _EVENT_MAP[_d] = "FOMC"
for _d in _CPI_2026:
    # FOMC takes precedence if dates collide (unlikely but possible)
    _EVENT_MAP.setdefault(_d, "CPI")
for _d in _NFP_2026:
    _EVENT_MAP.setdefault(_d, "NFP")

# Years for which this table actually has data. Queries outside this set can't
# be answered and must warn rather than silently return None — otherwise, from
# 2027 on, every FOMC/CPI/NFP day reads as "no macro event" and the blackout
# filter fails open (F-16).
_KNOWN_YEARS: frozenset[int] = frozenset({2026})
_warned_years: set[int] = set()


def is_macro_event_day(dt: date) -> Optional[str]:
    """
    Return the macro event name if the given date is a scheduled release,
    otherwise None.

    Args:
        dt: Calendar date to check (date or datetime — date portion used).

    Returns:
        One of 'FOMC', 'CPI', 'NFP', or None.
    """
    if hasattr(dt, "date"):
        dt = dt.date()  # type: ignore[union-attr]
    if dt.year not in _KNOWN_YEARS and dt.year not in _warned_years:
        _warned_years.add(dt.year)
        logger.warning(
            "macro_calendar has no data for %d (only %s) — macro-event blackout "
            "is INACTIVE for that year; refresh the FOMC/CPI/NFP tables.",
            dt.year, sorted(_KNOWN_YEARS),
        )
    return _EVENT_MAP.get(dt)


def get_all_macro_events() -> list[tuple[date, str]]:
    """
    Return all 2026 macro events sorted by date.

    Returns:
        List of (date, event_name) tuples.
    """
    return sorted(_EVENT_MAP.items())
