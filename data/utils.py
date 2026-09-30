"""
Shared utilities for data adapters.

Provides:
  - RateLimiter: sliding-window request throttle
  - FileCache: JSON-file cache with TTL, stored in .cache/

Used internally by adapters — not part of the public adapter interface.
"""

import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Sliding-window rate limiter.

    Tracks call timestamps in a deque. Before each call, prunes timestamps
    older than the window and sleeps if the window is full.

    Args:
        max_calls: Maximum number of calls allowed in the window.
        period_seconds: Length of the sliding window in seconds.

    Usage:
        limiter = RateLimiter(max_calls=8, period_seconds=1.0)  # 8/sec
        for symbol in universe:
            limiter.wait_if_needed()
            response = requests.get(url)
    """

    def __init__(self, max_calls: int, period_seconds: float) -> None:
        self._max_calls = max_calls
        self._period = period_seconds
        self._calls: deque[float] = deque()

    def wait_if_needed(self) -> None:
        """Block until making another call would not exceed the rate limit."""
        now = time.monotonic()
        # Prune expired timestamps
        while self._calls and self._calls[0] < now - self._period:
            self._calls.popleft()
        if len(self._calls) >= self._max_calls:
            sleep_for = self._period - (now - self._calls[0])
            if sleep_for > 0:
                logger.debug("Rate limit: sleeping %.2fs", sleep_for)
                time.sleep(sleep_for)
        self._calls.append(time.monotonic())

    @property
    def calls_in_window(self) -> int:
        """Current number of calls within the active window."""
        now = time.monotonic()
        return sum(1 for t in self._calls if t >= now - self._period)


class FileCache:
    """
    Simple JSON file cache with per-entry TTL.

    Stores entries as individual JSON files under cache_dir. Each file
    contains the value and a Unix timestamp for expiry checks.

    Args:
        cache_dir: Directory for cache files. Created if absent.

    Usage:
        cache = FileCache(".cache")
        cached = cache.get("sector_etfs", ttl_seconds=86400)
        if cached is None:
            data = fetch_sector_etfs()
            cache.set("sector_etfs", data)
    """

    def __init__(self, cache_dir: str | Path = ".cache") -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str, ttl_seconds: float) -> Any | None:
        """
        Return cached value if it exists and is within TTL, else None.

        Args:
            key: Cache key (used as filename, so keep it filename-safe).
            ttl_seconds: Age in seconds beyond which the entry is stale.

        Returns:
            Cached value, or None if missing/stale.
        """
        path = self._dir / f"{self._sanitize(key)}.json"
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - entry["cached_at"]
            if age > ttl_seconds:
                logger.debug("Cache stale (%.0fs old, TTL %.0fs): %s", age, ttl_seconds, key)
                return None
            return entry["value"]
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.warning("Cache read error for %s: %s", key, exc)
            return None

    def set(self, key: str, value: Any) -> None:
        """
        Write a value to the cache with the current timestamp.

        Args:
            key: Cache key.
            value: JSON-serialisable value to cache.
        """
        path = self._dir / f"{self._sanitize(key)}.json"
        try:
            path.write_text(
                json.dumps({"cached_at": time.time(), "value": value}, default=str),
                encoding="utf-8",
            )
        except (OSError, TypeError) as exc:
            logger.warning("Cache write error for %s: %s", key, exc)

    def invalidate(self, key: str) -> None:
        """Delete a cache entry."""
        path = self._dir / f"{self._sanitize(key)}.json"
        if path.exists():
            path.unlink()

    @staticmethod
    def _sanitize(key: str) -> str:
        """Replace characters that are invalid in filenames."""
        return key.replace("/", "_").replace("\\", "_").replace(":", "_")
