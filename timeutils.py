"""
UTC clock helper (F-33).

``datetime.utcnow()`` is deprecated from Python 3.12 and slated for removal.
``utcnow()`` here returns the same **naive** UTC datetime the codebase has always
stored — computed the non-deprecated way (``datetime.now(timezone.utc)`` with the
tzinfo stripped) — so every existing naive comparison and naive ``DateTime``
column keeps working unchanged. Do not return a tz-aware value here: that would
make stored timestamps aware and break naive-vs-aware comparisons across the DB.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (drop-in for datetime.utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
