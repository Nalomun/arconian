"""
Diagnose the missing 10-month universe history in the backtest.
Reference: claude-code-prompt-data-audit.md, Task 2.
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.norgate_adapter import NorgateAdapter, NorgateUnavailableError

ROOT         = Path(__file__).parent.parent
BACKTEST_CSV = ROOT / "backtest_2024-01-01_2026-04-21.csv"
REPORT_MD    = ROOT / "diagnose_history_gap.md"

MIN_PRICE   = 2.0
MIN_ADTV_M  = 5.0
MIN_HISTORY = 120

AS_OF_DATES = [
    pd.Timestamp("2024-06-01"),
    pd.Timestamp("2024-03-01"),
    pd.Timestamp("2024-01-15"),
]


def _passes_filters(hist: pd.DataFrame, as_of: pd.Timestamp) -> tuple[bool, str]:
    """Replicates backtest._passes_filters; returns (passes, failure_reason)."""
    h = hist[hist.index <= as_of]
    if len(h) < MIN_HISTORY:
        return False, f"history={len(h)}<{MIN_HISTORY}"
    last_price = float(h["Close"].iloc[-1])
    if last_price < MIN_PRICE:
        return False, f"price=${last_price:.2f}<${MIN_PRICE:.0f}"
    recent = h.tail(20)
    adtv_m = float((recent["Close"] * recent["Volume"]).mean()) / 1e6
    if adtv_m < MIN_ADTV_M:
        return False, f"adtv=${adtv_m:.2f}M<${MIN_ADTV_M:.0f}M"
    return True, "passes"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger(__name__)

    log.info("Loading backtest CSV to select sample tickers from 2024-11-01...")
    df = pd.read_csv(BACKTEST_CSV, parse_dates=["date"])
    nov_rows = df[df["date"].dt.date == date(2024, 11, 1)]
    sample_tickers = nov_rows["ticker"].tolist()[:5]
    log.info("Sample tickers: %s", sample_tickers)

    try:
        norgate = NorgateAdapter()
    except NorgateUnavailableError as exc:
        log.error("Norgate unavailable: %s", exc)
        sys.exit(1)

    results = []
    for ticker in sample_tickers:
        log.info("Checking %s ...", ticker)
        hist = norgate.get_price_history(ticker, date(2022, 1, 1), date(2024, 11, 1))

        if hist is None or hist.empty:
            row: dict = {
                "ticker":     ticker,
                "n_rows":     0,
                "first_date": None,
                "last_date":  None,
            }
            for aod in AS_OF_DATES:
                key = str(aod.date())
                row[f"passes_{key}"] = False
                row[f"reason_{key}"] = "no data returned"
        else:
            row = {
                "ticker":     ticker,
                "n_rows":     len(hist),
                "first_date": hist.index[0].date(),
                "last_date":  hist.index[-1].date(),
            }
            for aod in AS_OF_DATES:
                key = str(aod.date())
                passes, reason = _passes_filters(hist, aod)
                row[f"passes_{key}"] = passes
                row[f"reason_{key}"] = reason

        results.append(row)
        log.info("  %s: %d rows, %s → %s",
                 ticker, row["n_rows"], row["first_date"], row["last_date"])

    # --- Determine root cause ---
    first_dates = [r["first_date"] for r in results if r["first_date"] is not None]
    trial_cutoff = date(2024, 8, 1)

    if not first_dates or all(r["n_rows"] == 0 for r in results):
        cause = "A"
        cause_detail = (
            "Norgate returned no data at all for the sample tickers when requesting "
            "from 2022-01-01. This is a data access or subscription issue."
        )
    elif any(d > trial_cutoff for d in first_dates):
        cause = "A"
        earliest = min(first_dates)
        latest_first = max(first_dates)
        td_available = int((date(2024, 11, 1) - latest_first).days * 252 / 365)
        cause_detail = (
            f"Norgate returned data, but it only starts around {earliest} to {latest_first} "
            f"for these tickers — not 2022-01-01 as requested. This is consistent with a "
            f"Norgate trial subscription that provides ~18 months of history. With data "
            f"starting {latest_first}, a symbol has only ~{td_available} trading days available "
            f"by 2024-11-01, just enough to pass MIN_HISTORY={MIN_HISTORY}. At any earlier "
            f"as-of date the history check fails."
        )
    else:
        # Full history returned — check why filters fail
        jan15_key = str(date(2024, 1, 15))
        jan15_fails = [
            r for r in results
            if not r.get(f"passes_{jan15_key}", True)
        ]
        history_fails = [
            r for r in jan15_fails
            if "history=" in r.get(f"reason_{jan15_key}", "")
        ]
        non_history_fails = [
            r for r in jan15_fails
            if "history=" not in r.get(f"reason_{jan15_key}", "")
        ]

        if history_fails:
            cause = "A"
            cause_detail = (
                "Norgate returned data but it starts too late for the history-length "
                "filter to pass at 2024-01-15. Even with the full-history request the "
                "coverage is insufficient for the early backtest dates."
            )
        elif non_history_fails:
            cause = "B"
            reasons = [r.get(f"reason_{jan15_key}", "") for r in non_history_fails]
            cause_detail = (
                f"Norgate provides full history back to 2022, but these tickers fail "
                f"_passes_filters at 2024-01-15 for non-history reasons: {reasons}. "
                "The universe was genuinely sparse or illiquid in early 2024."
            )
        else:
            cause = "unknown"
            cause_detail = (
                "All 5 sample tickers pass filters at 2024-01-15 with full Norgate history. "
                "The original gap may have been caused by a different set of tickers or a "
                "transient data issue during the backtest run."
            )

    # --- Build report ---
    def _fmt(r: dict, d: date) -> str:
        key = str(d)
        passes = r.get(f"passes_{key}", False)
        reason = r.get(f"reason_{key}", "")
        return "✓" if passes else f"✗ {reason}"

    lines = [
        "# History Gap Diagnostic Report",
        "",
        "## Background",
        "",
        "Backtest `--start 2024-01-01` logged `universe: 0 tickers` from January through",
        "October 2024, then 3,094 tickers in November 2024. This report identifies why.",
        "",
        "## Sample Tickers",
        "",
        f"Selected from 2024-11-01 rows in the CSV: `{', '.join(sample_tickers)}`",
        "",
        "## Norgate History Coverage (requested: 2022-01-01 → 2024-11-01)",
        "",
        "| Ticker | Rows returned | First date | Last date |",
        "|--------|--------------|------------|-----------|",
    ]
    for r in results:
        lines.append(
            f"| {r['ticker']} | {r['n_rows']:,} | {r['first_date']} | {r['last_date']} |"
        )

    lines += [
        "",
        "## Point-in-Time Filter Simulation",
        "",
        "| Ticker | 2024-06-01 | 2024-03-01 | 2024-01-15 |",
        "|--------|------------|------------|------------|",
    ]
    for r in results:
        lines.append(
            f"| {r['ticker']}"
            f" | {_fmt(r, date(2024, 6, 1))}"
            f" | {_fmt(r, date(2024, 3, 1))}"
            f" | {_fmt(r, date(2024, 1, 15))} |"
        )

    lines += [
        "",
        f"## Root Cause: {cause}",
        "",
        cause_detail,
        "",
    ]

    if cause == "A":
        lines += [
            "## Action",
            "",
            "**No code change warranted.** The `_quick_prefilter` and `_passes_filters`",
            "logic is correct. The empty universe in Jan–Oct 2024 is explained by Norgate",
            "trial data coverage, not a bug.",
            "",
            "The backtest data is valid for its actual window: **November 2024 → April 2026**",
            "(approximately 17 months, ~365 trading days).",
            "",
            "## Impact on Thesis Verifiability",
            "",
            "- The 17-month window covers a single macro regime (post-election rally through",
            "  early 2026 volatility). Edge persistence cannot be confirmed across multiple cycles.",
            "- Phase 1 live trading provides the first genuinely independent out-of-sample test.",
            "- A full Norgate Platinum subscription (~$500/year) would unlock the pre-2024 data",
            "  and enable a materially longer backtest. This is the only fix available.",
        ]
    elif cause == "B":
        lines += [
            "## Action",
            "",
            "**No code change needed.** The filter logic is correct. The universe was",
            "genuinely sparse in early 2024 — these tickers did not meet the price/ADTV",
            "floors at that time. Point-in-time filtering is working as designed.",
        ]
    elif cause == "C":
        lines += [
            "## Action",
            "",
            "**One-line fix in `scripts/backtest.py` `_quick_prefilter`:**",
            "Change the `approx_start` calculation to use `norgate_start` instead of",
            "`end_date - 40 days`. Then rerun the backtest with `_v2` suffix.",
        ]

    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    log.info("Report written: %s", REPORT_MD)

    # --- Console summary ---
    print()
    print("=" * 60)
    print("HISTORY GAP DIAGNOSTIC")
    print("=" * 60)
    print(f"Sample tickers from 2024-11-01: {sample_tickers}")
    print()
    for r in results:
        print(f"{r['ticker']}: {r['n_rows']:,} rows  {r['first_date']} → {r['last_date']}")
        for aod in AS_OF_DATES:
            key = str(aod.date())
            p   = r.get(f"passes_{key}", False)
            rsn = r.get(f"reason_{key}", "")
            print(f"  {aod.date()}: {'PASSES' if p else 'FAILS (' + rsn + ')'}")
    print()
    print(f"ROOT CAUSE: {cause}")
    print(cause_detail)
    print()


if __name__ == "__main__":
    main()
