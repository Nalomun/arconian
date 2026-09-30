"""
SEC EDGAR API adapter.

Fetches recent filings, detects corporate actions from 8-K items, and
maintains a locally-cached CIK mapping (refreshed weekly).

Uses the SEC's free public API — no API key required, but a valid
User-Agent header containing a contact email is required by SEC policy.

Rate limit: ≤8 req/sec (conservative vs. SEC's 10/sec stated limit).
User-Agent: Set via EDGAR_USER_AGENT env var or the constructor argument.

All methods return None / empty list on failure — never raise to callers.

Whitepaper reference: Section 2.4 (Corporate Action Filtering)
Supplements reference: Doc 1, Section 1.2
"""

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import requests

from data.utils import FileCache, RateLimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EDGAR API endpoints
# ---------------------------------------------------------------------------
_EDGAR_BASE = "https://data.sec.gov"
_CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = _EDGAR_BASE + "/submissions/CIK{cik}.json"

# CIK map refresh TTL: 7 days
_CIK_MAP_TTL = 7 * 24 * 3600

# ---------------------------------------------------------------------------
# 8-K item detection patterns (Doc 1, Section 1.2)
#
# Keyed by SEC 8-K item number per the official form (NOT guesses). The old map
# was materially wrong (F-16): Item 1.01 — "Entry into a Material Definitive
# Agreement", an extremely common, often-routine filing — was tagged
# merger_acquisition with a 999-day (near-permanent) exclusion, and Item 8.01 —
# "Other Events", a catch-all — was tagged ticker_change. Wired as-is, ordinary
# 8-Ks would have suspended healthy tickers indefinitely.
#
# Ordering matters: search_8k_items takes the FIRST match per filing (one 8-K
# can list several items), so the most severe actions are listed first.
_ITEM_PATTERNS: dict[str, re.Pattern[str]] = {
    # Item 3.01 — Notice of Delisting / failure to satisfy a listing rule.
    "delisting":             re.compile(r"Item\s+3\.01", re.IGNORECASE),
    # Item 1.03 — Bankruptcy or Receivership.
    "bankruptcy":            re.compile(r"Item\s+1\.03", re.IGNORECASE),
    # Item 2.01 — Completion of Acquisition or Disposition of Assets (the actual
    # "merger/acquisition completed" item — NOT 1.01).
    "merger_acquisition":    re.compile(r"Item\s+2\.01", re.IGNORECASE),
    # Free-text reverse-split detection (usually filed under 5.03, but the
    # phrase can appear anywhere in the body).
    "reverse_split":         re.compile(
        r"reverse[\s\-]+(?:stock[\s\-]+)?split", re.IGNORECASE
    ),
    # Item 2.06 — Material Impairments.
    "material_impairment":   re.compile(r"Item\s+2\.06", re.IGNORECASE),
    # Item 5.03 — Amendments to Articles/Bylaws; where name/ticker changes are
    # reported (SEC has no dedicated "ticker change" item).
    "name_change":           re.compile(r"Item\s+5\.03", re.IGNORECASE),
    # Item 1.01 — Entry into a Material Definitive Agreement. Material but
    # frequently routine (financings, supply deals); brief blackout, not 999d.
    "material_agreement":    re.compile(r"Item\s+1\.01", re.IGNORECASE),
    # Item 8.01 — Other Events. Catch-all; mostly immaterial → minimal blackout.
    "other_events":          re.compile(r"Item\s+8\.01", re.IGNORECASE),
}

# Form types that trigger exclusion zones
_CORPORATE_ACTION_FORMS = {"S-3", "S-3/A", "8-K", "SC 13D", "F-4"}


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------

@dataclass
class FilingRecord:
    """A single SEC filing."""

    cik: str
    ticker: str
    form_type: str
    filing_date: date
    accession_number: str
    primary_document: str


@dataclass
class CorporateActionFlag:
    """A detected corporate action that may trigger a signal exclusion zone."""

    ticker: str
    action_type: str          # 'secondary', 'reverse_split', 'merger', 'delisting', etc.
    filing_date: date
    description: str
    days_exclusion: int       # how many calendar days to exclude signals


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class EdgarAdapter:
    """
    Adapter for the SEC EDGAR public filing API.

    Args:
        user_agent: Required by SEC (e.g. 'Arconian/1.0 (you@example.com)').
            Falls back to EDGAR_USER_AGENT env var.
        cache_dir: Directory for file cache.
    """

    def __init__(
        self,
        user_agent: Optional[str] = None,
        cache_dir: str = ".cache",
    ) -> None:
        self._user_agent = (
            user_agent
            or os.environ.get("EDGAR_USER_AGENT")
            or "Arconian/1.0 (trading-system@example.com)"
        )
        if "example.com" in self._user_agent:
            logger.warning(
                "EDGAR User-Agent uses placeholder email. "
                "Set EDGAR_USER_AGENT env var with a real contact email."
            )
        self._cache = FileCache(cache_dir)
        self._rate_limiter = RateLimiter(max_calls=8, period_seconds=1.0)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self._user_agent,
            "Accept-Encoding": "gzip, deflate",
        })
        logger.info("EdgarAdapter initialised (User-Agent: %s)", self._user_agent)

    # ------------------------------------------------------------------
    # CIK mapping
    # ------------------------------------------------------------------

    def get_cik(self, ticker: str) -> Optional[str]:
        """
        Return the 10-digit zero-padded CIK for a ticker symbol.

        CIK map is cached locally for 7 days.

        Args:
            ticker: Ticker symbol (case-insensitive).

        Returns:
            Zero-padded CIK string (e.g. '0001234567'), or None if not found.
        """
        cik_map = self._load_cik_map()
        if cik_map is None:
            return None
        entry = cik_map.get(ticker.upper())
        if entry is None:
            logger.debug("EDGAR: CIK not found for %s", ticker)
            return None
        return str(entry["cik_str"]).zfill(10)

    # ------------------------------------------------------------------
    # Filing lookups
    # ------------------------------------------------------------------

    def get_recent_filings(
        self,
        cik: str,
        form_types: list[str],
        lookback_days: int = 30,
    ) -> list[FilingRecord]:
        """
        Return recent filings of specified form types for a CIK.

        Args:
            cik: 10-digit CIK string.
            form_types: List of form types to filter, e.g. ['S-3', '8-K'].
            lookback_days: How many calendar days back to search.

        Returns:
            List of FilingRecord objects (may be empty).
        """
        submissions = self._fetch_submissions(cik)
        if submissions is None:
            return []

        cutoff = date.today() - timedelta(days=lookback_days)
        results: list[FilingRecord] = []

        try:
            recent = submissions.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accessions = recent.get("accessionNumber", [])
            docs = recent.get("primaryDocument", [])
            ticker = submissions.get("tickers", [None])[0]

            for form, filing_date_str, accession, doc in zip(forms, dates, accessions, docs):
                if form not in form_types:
                    continue
                try:
                    filing_date = date.fromisoformat(filing_date_str)
                except ValueError:
                    continue
                if filing_date < cutoff:
                    continue
                results.append(FilingRecord(
                    cik=cik,
                    ticker=ticker or "",
                    form_type=form,
                    filing_date=filing_date,
                    accession_number=accession,
                    primary_document=doc,
                ))
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("EDGAR filing parse error for CIK %s: %s", cik, exc)

        return results

    def search_8k_items(
        self,
        cik: str,
        lookback_days: int = 10,
    ) -> list[CorporateActionFlag]:
        """
        Scan recent 8-K filings for corporate action items.

        Uses regex patterns to detect key event types from filing text.

        Args:
            cik: 10-digit CIK string.
            lookback_days: How many calendar days back to search.

        Returns:
            List of CorporateActionFlag objects (may be empty).
        """
        filings = self.get_recent_filings(cik, ["8-K"], lookback_days=lookback_days)
        flags: list[CorporateActionFlag] = []

        for filing in filings:
            text = self._fetch_filing_text(filing.cik, filing.accession_number, filing.primary_document)
            if text is None:
                continue
            for action_type, pattern in _ITEM_PATTERNS.items():
                if pattern.search(text):
                    flags.append(CorporateActionFlag(
                        ticker=filing.ticker,
                        action_type=action_type,
                        filing_date=filing.filing_date,
                        description=f"{filing.form_type} filed {filing.filing_date}",
                        days_exclusion=self._exclusion_days(action_type),
                    ))
                    break  # One flag per 8-K filing is sufficient

        return flags

    def detect_corporate_actions(self, ticker: str) -> list[CorporateActionFlag]:
        """
        Full corporate action detection for a ticker: S-3 offerings + 8-K events.

        Args:
            ticker: Ticker symbol.

        Returns:
            List of active CorporateActionFlag objects (may be empty).
        """
        cik = self.get_cik(ticker)
        if cik is None:
            logger.debug("EDGAR: cannot detect corp actions for %s (CIK unknown)", ticker)
            return []

        flags: list[CorporateActionFlag] = []

        # Check for secondary offerings (S-3 filings within 5 days)
        s3_filings = self.get_recent_filings(cik, ["S-3", "S-3/A"], lookback_days=5)
        for f in s3_filings:
            flags.append(CorporateActionFlag(
                ticker=ticker,
                action_type="secondary",
                filing_date=f.filing_date,
                description=f"S-3 secondary offering filed {f.filing_date}",
                days_exclusion=5,
            ))

        # Check 8-K items
        flags.extend(self.search_8k_items(cik, lookback_days=10))

        return flags

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_cik_map(self) -> Optional[dict[str, dict]]:
        """Load CIK map from cache or SEC endpoint."""
        cached = self._cache.get("edgar_cik_map", ttl_seconds=_CIK_MAP_TTL)
        if cached is not None:
            return cached

        self._rate_limiter.wait_if_needed()
        try:
            resp = self._session.get(_CIK_MAP_URL, timeout=15)
            resp.raise_for_status()
            raw: dict[str, dict] = resp.json()
            # Key by ticker (uppercase) for fast lookup
            by_ticker: dict[str, dict] = {}
            for entry in raw.values():
                if "ticker" in entry:
                    by_ticker[entry["ticker"].upper()] = entry
            self._cache.set("edgar_cik_map", by_ticker)
            logger.info("EDGAR CIK map loaded (%d tickers)", len(by_ticker))
            return by_ticker
        except Exception as exc:
            logger.error("EDGAR CIK map fetch failed: %s", exc)
            return None

    def _fetch_submissions(self, cik: str) -> Optional[dict]:
        """Fetch the submissions JSON for a CIK."""
        cache_key = f"edgar_submissions_{cik}"
        cached = self._cache.get(cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached

        url = _SUBMISSIONS_URL.format(cik=cik)
        self._rate_limiter.wait_if_needed()
        try:
            resp = self._session.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            self._cache.set(cache_key, data)
            return data
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                logger.warning("EDGAR rate limited — backing off 60s")
                time.sleep(60)
            else:
                logger.warning("EDGAR submissions fetch failed for CIK %s: %s", cik, exc)
            return None
        except Exception as exc:
            logger.warning("EDGAR submissions fetch failed for CIK %s: %s", cik, exc)
            return None

    def _fetch_filing_text(
        self, cik: str, accession: str, primary_doc: str
    ) -> Optional[str]:
        """Fetch the text of a specific filing document."""
        accession_clean = accession.replace("-", "")
        url = (
            f"{_EDGAR_BASE}/Archives/edgar/data/{cik.lstrip('0')}/"
            f"{accession_clean}/{primary_doc}"
        )
        self._rate_limiter.wait_if_needed()
        try:
            resp = self._session.get(url, timeout=20)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            logger.warning("EDGAR filing text fetch failed (%s): %s", url, exc)
            return None

    @staticmethod
    def _exclusion_days(action_type: str) -> int:
        """Return the number of calendar days to exclude signals for this action type."""
        mapping = {
            "secondary":          5,
            "reverse_split":      3,
            "merger_acquisition": 999,  # deal pending → large sentinel
            "bankruptcy":         999,
            "delisting":          999,
            "name_change":        5,
            "material_agreement": 5,    # Item 1.01 — material but often routine
            "material_impairment": 3,
            "other_events":       1,    # Item 8.01 — catch-all, minimal blackout
        }
        return mapping.get(action_type, 5)
