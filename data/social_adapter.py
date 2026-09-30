"""
Social data adapter — StockTwits + Reddit mention velocity.

Provides the retail attention penalty inputs: social mention rate and
scanner-flag detection. Graceful degradation: if either source fails,
that component returns 0.0 (no penalty contribution from that source).

Rate limits (Doc 1, Section 1.3):
  - StockTwits: 200 req/hr unauthenticated
  - Reddit (PRAW): 60 req/min (OAuth)

Strategy: Only scan Pass 1 top-40 candidates per scan, NOT the full
universe. 40 calls × 3 scans/day = 120 calls/day — well within limits.

All methods return 0.0 on failure rather than None, so the composite
score degrades gracefully without requiring None-handling at every callsite.

Whitepaper reference: Section 2.5 (Retail Attention Penalty)
Supplements reference: Doc 1, Section 1.3
"""

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from data.utils import FileCache, RateLimiter

logger = logging.getLogger(__name__)

_STOCKTWITS_BASE = "https://api.stocktwits.com/api/2"
_SOCIAL_CACHE_TTL = 3600  # 1 hour — social velocity is a trailing 24h metric


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------

@dataclass
class SocialVelocity:
    """Retail attention signal inputs for a single ticker."""

    ticker: str
    stocktwits_mentions_24h: float = 0.0
    reddit_mentions_24h: float = 0.0
    is_trending_stocktwits: bool = False
    stocktwits_available: bool = True
    reddit_available: bool = True
    # Composite 0–1 score (computed by retail_attention.py, not here)
    # Included for logging / debugging
    raw_combined_score: float = 0.0


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class SocialAdapter:
    """
    Combined StockTwits + Reddit social mention adapter.

    Args:
        praw_client: Authenticated praw.Reddit instance. If None, Reddit
            component is disabled and returns 0 mentions.
        cache_dir: Directory for file cache.
    """

    def __init__(
        self,
        praw_client=None,  # praw.Reddit | None
        cache_dir: str = ".cache",
    ) -> None:
        self._praw = praw_client
        self._cache = FileCache(cache_dir)
        self._st_limiter = RateLimiter(max_calls=180, period_seconds=3600)  # ~200/hr with buffer
        self._reddit_limiter = RateLimiter(max_calls=55, period_seconds=60)  # ~60/min with buffer
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": "Arconian/1.0"})
        self._trending_cache: Optional[set[str]] = None
        self._trending_fetched_at: Optional[float] = None
        logger.info(
            "SocialAdapter initialised (Reddit: %s)",
            "enabled" if self._praw else "disabled",
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_stocktwits_mentions(self, symbol: str, hours: int = 24) -> float:
        """
        Return the count of StockTwits messages for a symbol in the trailing window.

        Uses the symbol stream endpoint. Paginates up to 3 pages (90 messages max)
        to count messages within the specified window.

        Args:
            symbol: Ticker symbol (without $ prefix).
            hours: Trailing window in hours.

        Returns:
            Message count (float for consistency with composite scoring), or 0.0 on failure.
        """
        count, _available = self._fetch_stocktwits_mentions(symbol, hours)
        return count

    def _fetch_stocktwits_mentions(
        self, symbol: str, hours: int = 24
    ) -> tuple[float, bool]:
        """
        Return ``(mention_count, available)`` for StockTwits.

        ``available`` is False when the fetch could not be trusted — a network
        error, a 429, or a blocked request (StockTwits 403s scripted clients).
        In that case the count is NOT a real "zero mentions" reading and must
        not be cached or treated as signal (F-35): caching 0 on failure made
        meme names — the very tickers the retail penalty exists for — silently
        read as quiet for an hour, and ``stocktwits_available`` was hardcoded
        True regardless. A genuine empty/404 result IS trustworthy (count 0,
        available True) and is cached normally.
        """
        cache_key = f"st_mentions_{symbol}_{hours}h"
        cached = self._cache.get(cache_key, ttl_seconds=_SOCIAL_CACHE_TTL)
        if cached is not None:
            # Only successful reads are ever cached, so a hit is trustworthy.
            return float(cached), True

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        count = 0
        max_id: Optional[int] = None
        available = True

        for page in range(3):  # max 3 pages = 90 messages
            url = f"{_STOCKTWITS_BASE}/streams/symbol/{symbol}.json"
            params: dict = {}
            if max_id is not None:
                params["max"] = max_id

            self._st_limiter.wait_if_needed()
            try:
                resp = self._http.get(url, params=params, timeout=10)
                if resp.status_code == 429:
                    logger.warning("StockTwits rate limited for %s", symbol)
                    available = False  # transient — not a real zero
                    break
                if resp.status_code == 404:
                    # Symbol not found on StockTwits — valid, just no activity.
                    break
                resp.raise_for_status()
                data = resp.json()
                messages = data.get("messages", [])
                if not messages:
                    break

                any_in_window = False
                for msg in messages:
                    created_at = self._parse_st_timestamp(msg.get("created_at", ""))
                    if created_at and created_at >= cutoff:
                        count += 1
                        any_in_window = True
                    max_id = msg.get("id", max_id)

                if not any_in_window:
                    break  # All remaining messages are older than window
            except Exception as exc:
                # Network error / 403 block / bad JSON — the read is untrusted.
                logger.warning("StockTwits mentions failed for %s: %s", symbol, exc)
                available = False
                break

        # Only cache trustworthy results so a transient failure doesn't pin a
        # fake 0 for the cache TTL.
        if available:
            self._cache.set(cache_key, count)
        return float(count), available

    def get_reddit_mentions(
        self,
        symbol: str,
        hours: int = 24,
        subreddits: Optional[list[str]] = None,
    ) -> float:
        """
        Return the count of Reddit posts mentioning a symbol in the trailing window.

        Uses r/all search with ticker as query (more efficient than per-subreddit).
        Filters results to relevant financial subreddits post-fetch.

        Args:
            symbol: Ticker symbol.
            hours: Trailing window in hours.
            subreddits: Subreddits to include. Defaults to the standard set.

        Returns:
            Post count (float), or 0.0 if Reddit is unavailable.
        """
        if self._praw is None:
            return 0.0

        target_subs = set(
            subreddits or ["wallstreetbets", "stocks", "pennystocks", "smallstreetbets", "options"]
        )
        cache_key = f"reddit_mentions_{symbol}_{hours}h"
        cached = self._cache.get(cache_key, ttl_seconds=_SOCIAL_CACHE_TTL)
        if cached is not None:
            return float(cached)

        cutoff_ts = time.time() - (hours * 3600)
        count = 0

        self._reddit_limiter.wait_if_needed()
        try:
            # Single search across r/all — more efficient than per-subreddit
            results = self._praw.subreddit("all").search(
                f"${symbol} OR {symbol}",
                sort="new",
                time_filter="day",
                limit=100,
            )
            for post in results:
                if post.created_utc < cutoff_ts:
                    break
                if post.subreddit.display_name.lower() in {s.lower() for s in target_subs}:
                    count += 1
        except Exception as exc:
            logger.warning("Reddit mentions failed for %s: %s", symbol, exc)
            self._cache.set(cache_key, 0)
            return 0.0

        self._cache.set(cache_key, count)
        return float(count)

    def get_social_velocity(self, symbol: str) -> SocialVelocity:
        """
        Return combined social velocity metrics for a symbol.

        Args:
            symbol: Ticker symbol.

        Returns:
            SocialVelocity dataclass with mention counts and flags.
        """
        st_mentions, st_available = self._fetch_stocktwits_mentions(symbol)
        reddit_mentions = self.get_reddit_mentions(symbol)
        is_trending = self.is_trending_on_stocktwits(symbol)

        # Simple combined score: normalised log-scale count
        import math
        combined = math.log1p(st_mentions + reddit_mentions * 2) / 10.0

        return SocialVelocity(
            ticker=symbol,
            stocktwits_mentions_24h=st_mentions,
            reddit_mentions_24h=reddit_mentions,
            is_trending_stocktwits=is_trending,
            # Report the TRUE availability so downstream fail-open logic can tell
            # "0 mentions" from "couldn't reach StockTwits" (F-35).
            stocktwits_available=st_available,
            reddit_available=self._praw is not None,
            raw_combined_score=min(combined, 1.0),
        )

    def is_trending_on_stocktwits(self, symbol: str) -> bool:
        """
        Check if a symbol is in the StockTwits trending list.

        The trending list is fetched once per hour (1 API call covers all symbols).

        Args:
            symbol: Ticker symbol.

        Returns:
            True if currently trending, False otherwise or on failure.
        """
        now = time.monotonic()
        if (
            self._trending_cache is None
            or self._trending_fetched_at is None
            or now - self._trending_fetched_at > 3600
        ):
            self._refresh_trending()

        if self._trending_cache is None:
            return False
        return symbol.upper() in self._trending_cache

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_trending(self) -> None:
        """Fetch the StockTwits trending symbols list (1 API call)."""
        self._st_limiter.wait_if_needed()
        try:
            resp = self._http.get(
                f"{_STOCKTWITS_BASE}/trending/symbols.json",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            symbols = {
                s.get("symbol", "").upper()
                for s in data.get("symbols", [])
                if s.get("symbol")
            }
            self._trending_cache = symbols
            self._trending_fetched_at = time.monotonic()
            logger.debug("StockTwits trending: %d symbols", len(symbols))
        except Exception as exc:
            logger.warning("StockTwits trending fetch failed: %s", exc)
            self._trending_cache = set()
            self._trending_fetched_at = time.monotonic()

    @staticmethod
    def _parse_st_timestamp(ts_str: str) -> Optional[datetime]:
        """Parse a StockTwits ISO 8601 timestamp to a timezone-aware datetime."""
        if not ts_str:
            return None
        try:
            # StockTwits format: '2024-01-15T14:30:00Z'
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            return None
