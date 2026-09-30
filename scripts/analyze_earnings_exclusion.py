"""
Applies §3.5.1 earnings proximity exclusion and return-cap sensitivity
analysis to the thesis subset (top composite decile × delay > 0.3).

Part 1 — Return-cap sensitivity: no API calls, runs instantly.
Part 2 — Earnings lookup: yfinance for thesis-subset tickers only (~3–5 min).
Part 3 — Combined table: every meaningful filter combination in one view.

Output: analysis_earnings_exclusion.md at project root.
"""
from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

ROOT         = Path(__file__).parent.parent
BACKTEST_CSV = ROOT / "backtest_2024-01-01_2026-04-21.csv"
SUMMARY_MD   = ROOT / "analysis_earnings_exclusion.md"

# ±3 calendar days ≈ ±1 trading day accounting for weekends (§3.5.1)
EARNINGS_PROXIMITY_CAL_DAYS = 3

RETURN_CAPS = [0.20, 0.50, 1.00, 2.00, float("inf")]
COST_BPS    = 120  # §5 long-side round-trip cost assumption


# ---------------------------------------------------------------------------
# Earnings date fetch (threaded, one ticker at a time)
# ---------------------------------------------------------------------------

def _fetch_one(ticker: str) -> tuple[str, Optional[list[date]]]:
    try:
        df = yf.Ticker(ticker).earnings_dates
        if df is None or df.empty:
            return ticker, None
        dates = []
        for ts in df.index:
            try:
                d = ts.date() if hasattr(ts, "date") else ts
                if isinstance(d, date):
                    dates.append(d)
            except Exception:
                pass
        return ticker, sorted(set(dates)) if dates else None
    except Exception:
        return ticker, None


def fetch_all_earnings(tickers: list[str], workers: int = 12) -> dict[str, Optional[list[date]]]:
    result: dict[str, Optional[list[date]]] = {}
    log = logging.getLogger(__name__)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, t): t for t in tickers}
        done = 0
        for future in as_completed(futures):
            ticker, dates = future.result()
            result[ticker] = dates
            done += 1
            if done % 50 == 0:
                log.info("  Earnings lookup: %d / %d ...", done, len(tickers))
    return result


# ---------------------------------------------------------------------------
# Proximity tagging
# ---------------------------------------------------------------------------

def tag_earnings_proximity(
    df: pd.DataFrame,
    earnings_map: dict[str, Optional[list[date]]],
    col: str = "days_to_nearest_earnings",
) -> pd.DataFrame:
    """Add a column with the minimum calendar-day distance to any earnings date."""
    df = df.copy()
    df[col] = np.nan

    for ticker, grp in df.groupby("ticker"):
        dates = earnings_map.get(str(ticker))
        if not dates:
            continue
        earnings_ordinals = np.array([d.toordinal() for d in dates], dtype=np.int32)
        signal_ordinals   = grp["date"].apply(lambda x: x.toordinal()).values.astype(np.int32)
        # shape: (n_signals, n_earnings) — broadcast and find row-wise min
        diffs = np.abs(signal_ordinals[:, None] - earnings_ordinals[None, :])
        df.loc[grp.index, col] = diffs.min(axis=1)

    return df


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mean_bps(series: pd.Series) -> float:
    return float(series.mean() * 10_000)


def median_bps(series: pd.Series) -> float:
    return float(series.median() * 10_000)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger(__name__)

    # ------------------------------------------------------------------ #
    # Load + segment                                                       #
    # ------------------------------------------------------------------ #
    log.info("Loading backtest CSV...")
    df = pd.read_csv(BACKTEST_CSV, parse_dates=["date"])
    log.info("Loaded %d rows", len(df))

    df["decile"] = pd.qcut(df["composite"], 10, labels=False) + 1
    thesis  = df[(df["decile"] == 10) & (df["delay"] > 0.3)].copy()
    top_all = df[df["decile"] == 10].copy()
    bottom  = df[df["decile"] == 1].copy()
    log.info("Thesis subset (top decile × delay > 0.3): %d rows, %d unique tickers",
             len(thesis), thesis["ticker"].nunique())

    # ------------------------------------------------------------------ #
    # Part 1: Return-cap sensitivity — no API                             #
    # ------------------------------------------------------------------ #
    log.info("Part 1: Return-cap sensitivity...")
    subsets = [
        ("thesis (top×delay>0.3)", thesis),
        ("top decile (all delay)", top_all),
        ("bottom decile",          bottom),
    ]
    cap_rows = []
    for cap in RETURN_CAPS:
        label = f"|ret| <= {cap:.0%}" if cap < float("inf") else "uncapped"
        for name, sub in subsets:
            filt = sub[sub["fwd_return_3d"].abs() <= cap] if cap < float("inf") else sub
            cap_rows.append({
                "cap":        label,
                "subset":     name,
                "n":          len(filt),
                "mean_bps":   round(mean_bps(filt["fwd_return_3d"]), 1),
                "median_bps": round(median_bps(filt["fwd_return_3d"]), 1),
            })
    cap_df = pd.DataFrame(cap_rows)

    # ------------------------------------------------------------------ #
    # Part 2: Earnings lookup — thesis tickers only                       #
    # ------------------------------------------------------------------ #
    unique_tickers = thesis["ticker"].unique().tolist()
    log.info("Part 2: Fetching earnings dates for %d unique tickers...", len(unique_tickers))
    earnings_map = fetch_all_earnings(unique_tickers)

    has_dates = sum(1 for v in earnings_map.values() if v is not None)
    coverage_pct = 100 * has_dates / len(unique_tickers) if unique_tickers else 0
    log.info("Coverage: %d / %d (%.0f%%)", has_dates, len(unique_tickers), coverage_pct)

    thesis = tag_earnings_proximity(thesis, earnings_map)
    n_proximity = int((thesis["days_to_nearest_earnings"] <= EARNINGS_PROXIMITY_CAL_DAYS).sum())
    n_unknown   = int(thesis["days_to_nearest_earnings"].isna().sum())
    log.info("Earnings-proximate rows (within %d cal days): %d (%.1f%%)",
             EARNINGS_PROXIMITY_CAL_DAYS, n_proximity, 100 * n_proximity / len(thesis))
    log.info("Unknown earnings date: %d (%.1f%%)", n_unknown, 100 * n_unknown / len(thesis))

    # ------------------------------------------------------------------ #
    # Part 3: Combined filter combinations                                #
    # ------------------------------------------------------------------ #
    # Earnings-excluded: remove ±3 cal-day rows; keep unknowns
    e_excl = thesis[
        thesis["days_to_nearest_earnings"].isna() |
        (thesis["days_to_nearest_earnings"] > EARNINGS_PROXIMITY_CAL_DAYS)
    ]
    # Same but only rows where we confirmed coverage
    e_excl_known = thesis[
        thesis["days_to_nearest_earnings"].notna() &
        (thesis["days_to_nearest_earnings"] > EARNINGS_PROXIMITY_CAL_DAYS)
    ]
    e_excl_cap100 = e_excl[e_excl["fwd_return_3d"].abs() <= 1.00]
    e_excl_cap50  = e_excl[e_excl["fwd_return_3d"].abs() <= 0.50]
    e_excl_cap20  = e_excl[e_excl["fwd_return_3d"].abs() <= 0.20]

    combined = [
        ("Original (no filters)",                              thesis),
        (f"Earnings excluded (±{EARNINGS_PROXIMITY_CAL_DAYS}d cal; unknowns kept)", e_excl),
        ("Earnings excluded (confirmed coverage only)",         e_excl_known),
        ("Earnings excl + |ret| <= 100%",                      e_excl_cap100),
        ("Earnings excl + |ret| <= 50%",                       e_excl_cap50),
        ("Earnings excl + |ret| <= 20%",                       e_excl_cap20),
    ]

    # ------------------------------------------------------------------ #
    # Build markdown                                                       #
    # ------------------------------------------------------------------ #
    lines = [
        "# Earnings Exclusion + Return-Cap Sensitivity Analysis",
        "",
        "**Thesis subset:** top composite decile × delay > 0.3",
        f"**Earnings proximity threshold:** ±{EARNINGS_PROXIMITY_CAL_DAYS} calendar days (≈ ±1 trading day, §3.5.1)",
        f"**Cost threshold:** {COST_BPS} bps (§5 long-side round-trip)",
        "",
        f"yfinance coverage: {has_dates} / {len(unique_tickers)} thesis tickers ({coverage_pct:.0f}%)",
        "",
        "---",
        "",
        "## Part 1: Return-Cap Sensitivity",
        "",
        "Effect of capping extreme returns on mean forward return.",
        "",
        "| Cap | Subset | n | Mean (bps) | Median (bps) |",
        "|-----|--------|---|-----------|-------------|",
    ]
    for _, r in cap_df.iterrows():
        lines.append(
            f"| {r['cap']} | {r['subset']} | {r['n']:,} | {r['mean_bps']:+.1f} | {r['median_bps']:+.1f} |"
        )

    lines += [
        "",
        "---",
        "",
        "## Part 2: Earnings Coverage",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Unique thesis tickers | {len(unique_tickers):,} |",
        f"| Tickers with yfinance earnings data | {has_dates:,} ({coverage_pct:.0f}%) |",
        f"| Thesis rows within ±{EARNINGS_PROXIMITY_CAL_DAYS} cal days of earnings | {n_proximity:,} ({100*n_proximity/len(thesis):.1f}%) |",
        f"| Thesis rows with unknown earnings date | {n_unknown:,} ({100*n_unknown/len(thesis):.1f}%) |",
        "",
        "---",
        "",
        "## Part 3: Combined Filter Analysis (thesis subset only)",
        "",
        f"| Filter | n | Mean (bps) | Median (bps) | After cost ({COST_BPS} bps) |",
        f"|--------|---|-----------|-------------|--------------------------|",
    ]
    for label, sub in combined:
        m   = round(mean_bps(sub["fwd_return_3d"]), 1)
        med = round(median_bps(sub["fwd_return_3d"]), 1)
        ac  = round(m - COST_BPS, 1)
        lines.append(f"| {label} | {len(sub):,} | {m:+.1f} | {med:+.1f} | {ac:+.1f} |")

    # Auto interpretation
    clean = e_excl_cap100
    clean_mean   = round(mean_bps(clean["fwd_return_3d"]), 1)
    clean_median = round(median_bps(clean["fwd_return_3d"]), 1)

    if clean_mean >= COST_BPS:
        verdict = (
            f"After earnings exclusion and ±100% return cap, the thesis-subset mean "
            f"({clean_mean:+.1f} bps) clears the {COST_BPS} bps cost threshold. "
            "The signal appears viable after removing structural confounders."
        )
    elif clean_mean > 0:
        verdict = (
            f"After earnings exclusion and ±100% return cap, the thesis-subset mean "
            f"({clean_mean:+.1f} bps) is positive but below the {COST_BPS} bps cost threshold "
            f"(after-cost: {clean_mean - COST_BPS:+.1f} bps). Median is {clean_median:+.1f} bps. "
            "The signal has information content but the edge does not cover friction in the "
            "current backtest universe. The live system adds options signal, retail penalty, and "
            "tighter spread/liquidity filters — these may improve the ratio, but treat as unconfirmed."
        )
    else:
        verdict = (
            f"After earnings exclusion and ±100% return cap, the thesis-subset mean "
            f"({clean_mean:+.1f} bps) is zero or negative. The apparent edge in the original "
            "analysis was entirely driven by catalyst events. The information-diffusion edge "
            "is not confirmed in this dataset."
        )

    lines += [
        "",
        "---",
        "",
        "## Interpretation",
        "",
        verdict,
    ]

    SUMMARY_MD.write_text("\n".join(lines), encoding="utf-8")
    log.info("Summary written: %s", SUMMARY_MD)

    # ------------------------------------------------------------------ #
    # Console output                                                       #
    # ------------------------------------------------------------------ #
    print()
    print("=" * 70)
    print("EARNINGS EXCLUSION + RETURN-CAP SENSITIVITY")
    print("=" * 70)
    print()
    print("Part 1: Return-cap sensitivity — thesis subset")
    print(f"  {'Cap':<22}  {'Mean':>8}  {'Median':>8}  {'n':>8}")
    print(f"  {'-'*22}  {'-'*8}  {'-'*8}  {'-'*8}")
    for _, r in cap_df[cap_df["subset"] == "thesis (top×delay>0.3)"].iterrows():
        print(f"  {r['cap']:<22}  {r['mean_bps']:>+7.1f}  {r['median_bps']:>+7.1f}  {r['n']:>8,}")
    print()
    print(f"Part 2: Earnings coverage")
    print(f"  {has_dates}/{len(unique_tickers)} tickers with data  |  "
          f"{n_proximity:,} proximity rows removed ({100*n_proximity/len(thesis):.1f}%)  |  "
          f"{n_unknown:,} unknown ({100*n_unknown/len(thesis):.1f}%)")
    print()
    print("Part 3: Combined filters — thesis subset")
    print(f"  {'Filter':<50}  {'Mean':>7}  {'Median':>7}  {'Post-cost':>9}")
    print(f"  {'-'*50}  {'-'*7}  {'-'*7}  {'-'*9}")
    for label, sub in combined:
        m   = round(mean_bps(sub["fwd_return_3d"]), 1)
        med = round(median_bps(sub["fwd_return_3d"]), 1)
        print(f"  {label:<50}  {m:>+6.1f}  {med:>+6.1f}  {m-COST_BPS:>+8.1f}")
    print()
    print("Verdict:", verdict)
    print()


if __name__ == "__main__":
    main()
