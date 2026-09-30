"""Tests for data/edgar_adapter.py."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.edgar_adapter import (
    EdgarAdapter,
    FilingRecord,
    CorporateActionFlag,
    _ITEM_PATTERNS,
)

FIXTURES = Path(__file__).parent / "fixtures"


def make_adapter(tmp_path) -> EdgarAdapter:
    return EdgarAdapter(
        user_agent="Arconian/1.0 (test@test.com)",
        cache_dir=str(tmp_path / "cache"),
    )


# ---------------------------------------------------------------------------
# Item pattern matching
# ---------------------------------------------------------------------------

class TestItemPatterns:
    def test_detects_reverse_split(self):
        text = "Pursuant to the reverse stock split effective January 15, 2024..."
        assert _ITEM_PATTERNS["reverse_split"].search(text)

    def test_item_101_is_material_agreement_not_merger(self):
        """F-16: Item 1.01 is 'Entry into a Material Definitive Agreement',
        not inherently a merger. It must NOT match merger_acquisition (which
        carries a 999-day exclusion)."""
        text = "Item 1.01 Entry into a Material Definitive Agreement..."
        assert _ITEM_PATTERNS["material_agreement"].search(text)
        assert not _ITEM_PATTERNS["merger_acquisition"].search(text)

    def test_merger_detected_via_item_201(self):
        """F-16: the real 'acquisition completed' item is 2.01."""
        text = "Item 2.01 Completion of Acquisition or Disposition of Assets"
        assert _ITEM_PATTERNS["merger_acquisition"].search(text)

    def test_detects_delisting_via_item_301(self):
        text = "Item 3.01 Notice of Delisting or Failure to Satisfy a Continued Listing Rule"
        assert _ITEM_PATTERNS["delisting"].search(text)

    def test_item_801_is_other_events_not_ticker_change(self):
        """F-16: Item 8.01 is the 'Other Events' catch-all, not a ticker change.
        There is no longer a 'ticker_change' action type."""
        text = "Item 8.01 Other Events: routine corporate announcement."
        assert _ITEM_PATTERNS["other_events"].search(text)
        assert "ticker_change" not in _ITEM_PATTERNS

    def test_name_change_detected_via_item_503(self):
        """F-16: name/ticker changes are filed under Item 5.03."""
        text = "Item 5.03 Amendments to Articles of Incorporation — name change."
        assert _ITEM_PATTERNS["name_change"].search(text)

    def test_does_not_false_positive_on_normal_8k(self):
        text = "Item 2.02 Results of Operations and Financial Condition"
        for name, pattern in _ITEM_PATTERNS.items():
            assert not pattern.search(text), f"{name} false-matched a 2.02 filing"


# ---------------------------------------------------------------------------
# CIK mapping (with mocked HTTP)
# ---------------------------------------------------------------------------

class TestCikMapping:
    def test_get_cik_from_cached_map(self, tmp_path):
        adapter = make_adapter(tmp_path)

        # Pre-populate the cache
        import time, json
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "edgar_cik_map.json").write_text(json.dumps({
            "cached_at": time.time(),
            "value": {
                "SOFI": {"cik_str": 1418819, "ticker": "SOFI", "title": "SoFi Technologies"}
            }
        }))

        cik = adapter.get_cik("SOFI")
        assert cik == "0001418819"

    def test_get_cik_pads_to_10_digits(self, tmp_path):
        adapter = make_adapter(tmp_path)

        import time, json
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "edgar_cik_map.json").write_text(json.dumps({
            "cached_at": time.time(),
            "value": {
                "TINY": {"cik_str": 42, "ticker": "TINY", "title": "Tiny Co"}
            }
        }))

        cik = adapter.get_cik("TINY")
        assert cik == "0000000042"

    def test_get_cik_returns_none_for_unknown_ticker(self, tmp_path):
        adapter = make_adapter(tmp_path)

        import time, json
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "edgar_cik_map.json").write_text(json.dumps({
            "cached_at": time.time(),
            "value": {}
        }))

        assert adapter.get_cik("ZZZZZ") is None


# ---------------------------------------------------------------------------
# Recent filings (with mocked HTTP)
# ---------------------------------------------------------------------------

class TestRecentFilings:
    def _load_submission(self, tmp_path) -> EdgarAdapter:
        adapter = make_adapter(tmp_path)
        raw = json.loads((FIXTURES / "edgar_submission.json").read_text())
        # Pre-populate the cache so no HTTP call is made
        import time, json as json_mod
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "edgar_submissions_0001418819.json").write_text(json_mod.dumps({
            "cached_at": time.time(),
            "value": raw
        }))
        return adapter

    def test_finds_s3_filing(self, tmp_path):
        adapter = self._load_submission(tmp_path)
        # Use large lookback to cover fixture dates (2023–2024) relative to test run date
        filings = adapter.get_recent_filings("0001418819", ["S-3"], lookback_days=9999)
        assert any(f.form_type == "S-3" for f in filings)

    def test_finds_8k_filings(self, tmp_path):
        adapter = self._load_submission(tmp_path)
        filings = adapter.get_recent_filings("0001418819", ["8-K"], lookback_days=9999)
        assert len(filings) >= 2
        assert all(f.form_type == "8-K" for f in filings)

    def test_respects_lookback_days(self, tmp_path):
        # With lookback_days=1, no filings should match (all > 1 day old)
        adapter = self._load_submission(tmp_path)
        filings = adapter.get_recent_filings("0001418819", ["8-K"], lookback_days=1)
        # All fixture filings are from 2024, so none are within 1 day of today (2026)
        assert filings == []

    def test_returns_empty_on_unknown_cik(self, tmp_path):
        adapter = make_adapter(tmp_path)
        with patch.object(adapter, "_fetch_submissions", return_value=None):
            result = adapter.get_recent_filings("9999999999", ["8-K"])
        assert result == []


# ---------------------------------------------------------------------------
# Exclusion days
# ---------------------------------------------------------------------------

def test_exclusion_days_secondary():
    assert EdgarAdapter._exclusion_days("secondary") == 5

def test_exclusion_days_reverse_split():
    assert EdgarAdapter._exclusion_days("reverse_split") == 3

def test_exclusion_days_merger_large():
    assert EdgarAdapter._exclusion_days("merger_acquisition") > 100  # duration_of_deal sentinel

def test_exclusion_days_material_agreement_is_brief():
    """F-16: Item 1.01 must be a brief blackout, not the old 999-day halt."""
    assert EdgarAdapter._exclusion_days("material_agreement") == 5

def test_exclusion_days_other_events_minimal():
    """F-16: Item 8.01 catch-all gets a minimal blackout."""
    assert EdgarAdapter._exclusion_days("other_events") == 1

def test_exclusion_days_name_change():
    assert EdgarAdapter._exclusion_days("name_change") == 5
