"""
scripts/seed_universe.py — One-time (and re-runnable) universe seeding script.

Candidate sources (tried in order, results merged):
  1. iShares IWM holdings CSV  — full Russell 2000 (~1936 US equity tickers)
  2. iShares IJR holdings CSV  — full S&P SmallCap 600 (~647 tickers, partial overlap)
  3. SEC EDGAR company_tickers — fallback if both iShares downloads fail

All candidates are fed to UniverseManager.run_monthly_refresh(), which applies
the 6 universe filters (market cap, daily $vol, midday $vol, spread, options OI,
trading history) and writes passing tickers to universe_state as ACTIVE/OBSERVATION.

Without Schwab, spread/options/midday checks are inconclusive (treated as pass).
With Schwab active (default), only tickers with liquid options chains will pass.

Usage:
    python scripts/seed_universe.py               # full seed
    python scripts/seed_universe.py --dry-run     # print candidates, skip DB write
    python scripts/seed_universe.py --clean       # remove known-bad tickers first
    python scripts/seed_universe.py --no-schwab   # skip Schwab checks (permissive)
"""

import argparse
import csv
import json
import logging
import random
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import yfinance as yf

# Suppress noisy adapter logs during the long seed run
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("seed_universe")
logger.setLevel(logging.INFO)

# iShares holdings CSV endpoints (product IDs confirmed 2026-04-10)
_ISHARES_URLS = {
    "IWM": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",
    "IJR": "https://www.ishares.com/us/products/239774/ishares-core-sp-small-cap-etf/1467271812596.ajax?fileType=csv&fileName=IJR_holdings&dataType=fund",
}

# Market cap band for SEC fallback screening (millions)
_CAP_MIN_MM = 500
_CAP_MAX_MM = 2_000

# Tickers known to be non-equities (CEFs, preferred, etc.) that slipped through
_KNOWN_BAD = {"BH-A", "HYT", "CRF"}


# ---------------------------------------------------------------------------
# Candidate generators
# ---------------------------------------------------------------------------

def _fetch_ishares_csv(label: str, url: str) -> list[str]:
    """Download an iShares holdings CSV and return US equity tickers."""
    print(f"  [{label}] Downloading from iShares...", end=" ", flush=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8-sig", errors="replace")
    except Exception as exc:
        print(f"FAILED ({exc})")
        return []

    lines = raw.splitlines()
    try:
        header_idx = next(i for i, l in enumerate(lines) if l.startswith("Ticker,"))
    except StopIteration:
        print("FAILED (no header row found)")
        return []

    rows = list(csv.DictReader(lines[header_idx:]))
    tickers = [
        r["Ticker"].strip()
        for r in rows
        if r.get("Ticker") and r.get("Asset Class") and r.get("Location")
        and r["Asset Class"].strip() == "Equity"
        and r["Location"].strip() == "United States"
        and r["Ticker"].strip()
    ]
    print(f"{len(tickers)} US equity tickers")
    return tickers


def _fetch_sec_fallback(target: int, max_check: int = 4000) -> list[str]:
    """SEC company_tickers.json + yfinance fast_info market cap screen."""
    print(f"\n[SEC Fallback] Targeting {target} candidates...")
    url = "https://www.sec.gov/files/company_tickers.json"
    req = urllib.request.Request(url, headers={"User-Agent": "Arconian/1.0 (contact@arconian.local)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = json.loads(r.read())
    except Exception as exc:
        print(f"  SEC download failed: {exc}")
        return []

    # Filter heuristics: common stock symbols are short, no dots, no
    # typical non-equity suffixes (W=warrant, R=right, U=unit, Q=bankrupt,
    # P/^=preferred)
    _bad_suffixes = {"W", "R", "U", "Q"}
    all_tickers = [
        v["ticker"].upper()
        for v in raw.values()
        if (
            v.get("ticker")
            and 1 <= len(v["ticker"]) <= 5
            and "." not in v["ticker"]
            and not v["ticker"][-1] in _bad_suffixes
            and not v["ticker"].isdigit()
        )
    ]
    print(f"  {len(all_tickers)} SEC tickers after symbol filter")

    sample = all_tickers[:max_check]
    random.shuffle(sample)

    candidates: list[str] = []
    checked = 0
    print(f"  Checking market caps (up to {len(sample)} tickers)...")
    for ticker in sample:
        if len(candidates) >= target:
            break
        try:
            fi = yf.Ticker(ticker).fast_info
            mc = getattr(fi, "market_cap", None)
            if mc is not None:
                mc_mm = mc / 1_000_000
                if _CAP_MIN_MM <= mc_mm <= _CAP_MAX_MM:
                    candidates.append(ticker)
        except Exception:
            pass
        checked += 1
        if checked % 100 == 0:
            print(f"    {checked}/{len(sample)} checked | {len(candidates)} in range", end="\r")
        time.sleep(0.2)

    print(f"\n  {len(candidates)} tickers in ${_CAP_MIN_MM}M–${_CAP_MAX_MM}M range")
    return candidates


def build_candidate_list() -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(tickers: list[str]) -> None:
        for t in tickers:
            if t and t not in seen and t not in _KNOWN_BAD:
                seen.add(t)
                ordered.append(t)

    print("\n--- iShares holdings ---")
    for label, url in _ISHARES_URLS.items():
        _add(_fetch_ishares_csv(label, url))

    print(f"\nAfter iShares: {len(seen)} unique US equity candidates")

    if len(seen) < 200:
        # iShares downloads failed — use SEC fallback
        _add(_fetch_sec_fallback(target=800 - len(seen)))

    print(f"Final candidate list: {len(ordered)} tickers")
    return ordered


# ---------------------------------------------------------------------------
# Database cleanup
# ---------------------------------------------------------------------------

def clean_bad_tickers(universe_mgr) -> None:
    """Remove known non-equity tickers that slipped through the previous seed."""
    from db import session_scope
    from models import UniverseState

    with session_scope() as session:
        present = {
            r.ticker for r in session.query(UniverseState.ticker).all()
        }

    to_remove = _KNOWN_BAD & present
    if not to_remove:
        print("Clean: no bad tickers found in universe.")
        return

    for ticker in sorted(to_remove):
        universe_mgr.remove_ticker(ticker, reason="Non-equity removed during universe cleanup")
        print(f"  Removed {ticker}")
    print(f"Clean: removed {len(to_remove)} bad ticker(s)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the Arconian universe")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print candidates without writing to DB")
    parser.add_argument("--clean", action="store_true",
                        help="Remove known non-equity tickers before seeding")
    parser.add_argument("--no-schwab", action="store_true",
                        help="Skip Schwab adapter (permissive seed — more tickers pass)")
    parser.add_argument("--config", default="config/arconian_config.yaml")
    args = parser.parse_args()

    print("=" * 60)
    print("Arconian Universe Seeder")
    print("=" * 60)

    from config.config_loader import ConfigLoader
    from data.yfinance_adapter import YFinanceAdapter
    from db import init_db
    from universe.universe_manager import UniverseManager

    config = ConfigLoader(args.config)
    init_db()
    yf_adapter = YFinanceAdapter()

    schwab = None
    if not args.no_schwab:
        try:
            from data.schwab_adapter import SchwabAdapter
            schwab = SchwabAdapter.from_token_file()
            print("Schwab loaded — spread/options checks active")
        except Exception as exc:
            print(f"Schwab unavailable ({exc}) — spread/options checks skipped")
    else:
        print("--no-schwab: spread/options/midday checks skipped (permissive)")

    universe_mgr = UniverseManager(config=config, yf_adapter=yf_adapter, schwab_adapter=schwab)

    if args.clean:
        print("\n--- Cleaning bad tickers ---")
        clean_bad_tickers(universe_mgr)

    candidates = build_candidate_list()

    if not candidates:
        print("\nERROR: No candidates generated. Check network connectivity.")
        return 1

    if args.dry_run:
        print(f"\nDry-run — {len(candidates)} candidates (not written to DB)")
        for i in range(0, min(60, len(candidates)), 10):
            print("  " + "  ".join(candidates[i:i+10]))
        if len(candidates) > 60:
            print(f"  ... and {len(candidates) - 60} more")
        return 0

    print(f"\n{'=' * 60}")
    print(f"Running monthly refresh on {len(candidates)} candidates...")
    print(f"(3 yfinance + optional Schwab calls per ticker — may take 60-90 min)")
    print(f"{'=' * 60}\n")

    outcomes = universe_mgr.run_monthly_refresh(candidates)

    counts = Counter(outcomes.values())
    active = sorted(t for t, o in outcomes.items() if o == "added_active")
    observation = sorted(t for t, o in outcomes.items() if o == "added_observation")

    print(f"\n{'=' * 60}")
    print("Universe Seed Complete")
    print(f"{'=' * 60}")
    print(f"  Candidates screened :   {len(candidates)}")
    print(f"  Added ACTIVE        :   {len(active)}")
    print(f"  Added OBSERVATION   :   {len(observation)}")
    print(f"  Failed filters      :   {counts.get('skipped_failed_filters', 0)}")
    print(f"  Insufficient history:   {counts.get('skipped_insufficient_history', 0)}")
    print(f"  Already tracked     :   {counts.get('skipped_already_tracked', 0)}")
    print(f"  Blocked             :   {counts.get('skipped_blocked', 0)}")

    if active:
        print(f"\nFirst 20 ACTIVE tickers:")
        for t in active[:20]:
            print(f"  {t}")

    if observation:
        print(f"\nFirst 10 OBSERVATION tickers:")
        for t in observation[:10]:
            print(f"  {t}")

    if len(active) + len(observation) == 0:
        print("\nWARNING: Nothing added.")
        print("Try --no-schwab for a permissive seed (options check skipped).")
    else:
        print(f"\nNext step: python main.py --dry-run")

    return 0


if __name__ == "__main__":
    sys.exit(main())
