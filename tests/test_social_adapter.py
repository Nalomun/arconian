"""Tests for data/social_adapter.py — StockTwits fail-open handling (F-35)."""

import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.social_adapter import SocialAdapter


def _make_adapter(tmp_path) -> SocialAdapter:
    return SocialAdapter(praw_client=None, cache_dir=str(tmp_path / "cache"))


def _resp(status_code=200, payload=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload if payload is not None else {"messages": []}
    r.raise_for_status = MagicMock()
    return r


class TestStockTwitsAvailability:
    def test_network_failure_marks_unavailable_and_does_not_cache(self, tmp_path):
        """F-35: a failed fetch must report available=False and NOT cache 0."""
        adapter = _make_adapter(tmp_path)
        adapter._http = MagicMock()
        adapter._http.get.side_effect = ConnectionError("blocked / 403")

        count, available = adapter._fetch_stocktwits_mentions("GME")
        assert count == 0.0
        assert available is False
        # Nothing trustworthy was cached, so the next call retries (hits HTTP again).
        adapter._http.get.side_effect = ConnectionError("still blocked")
        adapter._fetch_stocktwits_mentions("GME")
        assert adapter._http.get.call_count >= 2

    def test_rate_limited_marks_unavailable(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        adapter._http = MagicMock()
        adapter._http.get.return_value = _resp(status_code=429)

        count, available = adapter._fetch_stocktwits_mentions("AMC")
        assert count == 0.0
        assert available is False

    def test_genuine_empty_is_available_and_cached(self, tmp_path):
        """A real 'no activity' result (empty messages) is trustworthy."""
        adapter = _make_adapter(tmp_path)
        adapter._http = MagicMock()
        adapter._http.get.return_value = _resp(status_code=200, payload={"messages": []})

        count, available = adapter._fetch_stocktwits_mentions("AAPL")
        assert count == 0.0
        assert available is True
        # Cached — a second call serves from cache without another HTTP hit.
        adapter._fetch_stocktwits_mentions("AAPL")
        assert adapter._http.get.call_count == 1

    def test_social_velocity_reflects_true_availability(self, tmp_path):
        """F-35: stocktwits_available must mirror the real fetch outcome, not be
        hardcoded True."""
        adapter = _make_adapter(tmp_path)
        adapter._http = MagicMock()
        adapter._http.get.side_effect = ConnectionError("403")

        sv = adapter.get_social_velocity("GME")
        assert sv.stocktwits_available is False
