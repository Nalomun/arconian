"""
Audit extreme forward returns in the backtest CSV.
Reference: claude-code-prompt-data-audit.md, Task 1.
"""
from __future__ import annotations

import logging
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from data.norgate_adapter import NorgateAdapter, NorgateUnavailableError

ROOT         = Path(__file__).parent.parent
BACKTEST_CSV = ROOT / "backtest_2024-01-01_2026-04-21.csv"
AUDIT_CSV    = ROOT / "audit_extreme_returns.csv"
SUMMARY_MD   = ROOT / "audit_extreme_returns_summary.md"

EXTREME_THRESHOLD = 0.20   # |fwd_return_3d| > 20%
MATCH_TOL         = 0.05   # ±5 percentage-point absolute difference
GAP_THRESHOLD     = 0.50   # single-day |pct change| > 50%
CLEAN_RATIOS      = [2.0, 3.0, 4.0, 5.0, 0.5, 1/3, 0.25, 0.2, 0.1]
CLEAN_RATIO_TOL   = 0.07   # within 7% of a clean split ratio


def _is_clean_split(ratio: float) -> tuple[bool, float | None]:
    for cr in CLEAN_RATIOS:
        if abs(ratio / cr - 1.0) < CLEAN_RATIO_TOL:
            return True, cr
    return False, None


def _classify_row(row: pd.Series, norgate: NorgateAdapter) -> dict:
    ticker   = str(row["ticker"])
    sig_date = row["date"].date()
    csv_ret  = float(row["fwd_return_3d"])

    base = {
        "ticker":                   ticker,
        "date":                     sig_date,
        "composite":                row.get("composite"),
        "delay":                    row.get("delay"),
        "fwd_return_3d_csv":        csv_ret,
        "fwd_return_3d_recomputed": None,
        "classification":           "norgate_lookup_failed",
        "notes":                    "",
    }

    hist = norgate.get_price_history(
        ticker,
        sig_date - timedelta(days=14),
        sig_date + timedelta(days=30),
    )
    if hist is None or hist.empty:
        base["notes"] = "Norgate returned no data"
        return base

    closes = hist["Close"].copy()
    closes.index = pd.to_datetime(closes.index)
    tds = list(closes.index)

    # Entry: last close on or before sig_date
    entry_candidates = [t for t in tds if t.date() <= sig_date]
    if not entry_candidates:
        base["notes"] = "no entry date found in window"
        return base
    entry_ts    = entry_candidates[-1]
    entry_price = float(closes[entry_ts])
    entry_pos   = tds.index(entry_ts)

    # Exit: 3 trading days after entry
    exit_pos = entry_pos + 3
    if exit_pos >= len(tds):
        base["notes"] = "insufficient forward data for 3-day exit"
        return base
    exit_price = float(closes.iloc[exit_pos])
    recomputed = exit_price / entry_price - 1.0
    base["fwd_return_3d_recomputed"] = round(recomputed, 6)

    # Detect large single-day moves in the lookup window
    pct_ch     = closes.pct_change().abs()
    large_gaps = pct_ch[pct_ch > GAP_THRESHOLD]
    gap_dates  = list(large_gaps.index)

    # Primary match: ±5 percentage points (absolute)
    matches = abs(recomputed - csv_ret) <= MATCH_TOL

    if matches:
        if gap_dates:
            base["classification"] = "real_move"
            base["notes"] = f"large gap {gap_dates[0].date()} but return confirmed by Norgate"
        else:
            base["classification"] = "real_move"
        return base

    # Return mismatch — check for split artifact
    if gap_dates:
        split_notes = []
        for gd in gap_dates:
            gd_pos = tds.index(gd)
            if gd_pos == 0:
                continue
            prev = float(closes.iloc[gd_pos - 1])
            if prev < 1e-9:
                continue
            ratio = float(closes[gd]) / prev
            is_split, cr = _is_clean_split(ratio)
            if is_split:
                split_notes.append(f"{gd.date()}:ratio={ratio:.3f}≈{cr}")
        if split_notes:
            base["classification"] = "split_artifact"
            base["notes"] = "; ".join(split_notes)
            return base

    base["classification"] = "computation_mismatch"
    base["notes"] = f"recomputed={recomputed:.4f} csv={csv_ret:.4f}"
    return base


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger(__name__)

    log.info("Loading backtest CSV...")
    df = pd.read_csv(BACKTEST_CSV, parse_dates=["date"])
    log.info("Loaded %d rows", len(df))

    extreme = df[df["fwd_return_3d"].abs() > EXTREME_THRESHOLD].copy()
    log.info("Extreme rows (|fwd_return_3d| > %.0f%%): %d",
             EXTREME_THRESHOLD * 100, len(extreme))

    try:
        norgate = NorgateAdapter()
    except NorgateUnavailableError as exc:
        log.error("Norgate unavailable: %s", exc)
        sys.exit(1)

    records = []
    for i, (_, row) in enumerate(extreme.iterrows()):
        if i % 20 == 0:
            log.info("  Classifying %d / %d ...", i, len(extreme))
        records.append(_classify_row(row, norgate))

    audit_df = pd.DataFrame(records)
    audit_df.to_csv(AUDIT_CSV, index=False)
    log.info("Audit CSV written: %s", AUDIT_CSV)

    # --- Summary stats ---
    total    = len(audit_df)
    counts   = audit_df["classification"].value_counts()
    n_real   = int(counts.get("real_move", 0))
    real_frac = n_real / total if total > 0 else 0.0

    # Recompute Analysis 3 means with non-real rows excluded
    artifact_keys = set(
        zip(
            audit_df.loc[audit_df["classification"] != "real_move", "ticker"].astype(str),
            audit_df.loc[audit_df["classification"] != "real_move", "date"],
        )
    )
    df["_key"] = list(zip(df["ticker"].astype(str), df["date"].dt.date))
    df_clean   = df[~df["_key"].isin(artifact_keys)].drop(columns=["_key"])
    df_clean["decile"] = pd.qcut(df_clean["composite"], 10, labels=False) + 1

    def _subset_mean_bps(mask: pd.Series) -> float:
        sub = df_clean.loc[mask, "fwd_return_3d"].dropna()
        return float(sub.mean() * 10_000) if len(sub) > 0 else float("nan")

    thesis_mean  = _subset_mean_bps((df_clean["decile"] == 10) & (df_clean["delay"] > 0.3))
    top_dec_mean = _subset_mean_bps(df_clean["decile"] == 10)
    bot_dec_mean = _subset_mean_bps(df_clean["decile"] == 1)

    # Top 10 real moves
    real_df     = audit_df[audit_df["classification"] == "real_move"].copy()
    real_df["abs_ret"] = real_df["fwd_return_3d_csv"].abs()
    top10 = real_df.nlargest(10, "abs_ret")

    # --- Build summary markdown ---
    lines = [
        "# Extreme Forward Return Audit Summary",
        "",
        f"**Backtest file:** backtest_2024-01-01_2026-04-21.csv",
        f"**Threshold:** |fwd_return_3d| > {EXTREME_THRESHOLD:.0%}",
        f"**Total extreme observations:** {total}",
        "",
        "## Classification Counts",
        "",
    ]
    for cls, cnt in counts.items():
        lines.append(f"- **{cls}:** {cnt} ({cnt/total:.1%})")
    lines += [
        "",
        f"**Confirmed real moves:** {n_real} ({real_frac:.1%})",
        f"**Artifacts / unconfirmed:** {total - n_real} ({1 - real_frac:.1%})",
        "",
        "## Top 10 Real Moves by Absolute Return",
        "",
        "| Ticker | Date | Return | Notes |",
        "|--------|------|--------|-------|",
    ]
    for _, r in top10.iterrows():
        lines.append(
            f"| {r['ticker']} | {r['date']} | {r['fwd_return_3d_csv']*100:+.1f}% | {r['notes']} |"
        )

    def _verdict(clean: float, original: float, tol: float) -> str:
        return "YES" if abs(clean - original) < tol else "NO — MATERIALLY CHANGED"

    lines += [
        "",
        "## Recomputed Analysis 3 Means (artifacts excluded)",
        "",
        "| Subset | Original | Clean | Delta | Reliable? |",
        "|--------|----------|-------|-------|-----------|",
        f"| Top decile × delay > 0.3 | +55.2 bps | {thesis_mean:+.1f} bps"
        f" | {thesis_mean - 55.2:+.1f} | {_verdict(thesis_mean, 55.2, 15)} |",
        f"| Top decile (all delay)   | +30.5 bps | {top_dec_mean:+.1f} bps"
        f" | {top_dec_mean - 30.5:+.1f} | {_verdict(top_dec_mean, 30.5, 10)} |",
        f"| Bottom decile            | +17.3 bps | {bot_dec_mean:+.1f} bps"
        f" | {bot_dec_mean - 17.3:+.1f} | {_verdict(bot_dec_mean, 17.3, 10)} |",
        "",
        "## Verdict: Are the original analysis means reliable?",
        "",
    ]
    if real_frac >= 0.85:
        lines.append(
            f"**YES** — {real_frac:.1%} of extreme observations are confirmed real moves (Norgate "
            "total-return data agrees with the CSV return within ±5 pp). The mean/median gap in "
            "Analysis 3 reflects genuine fat-tailed distributions (biotech/M&A/earnings shocks), "
            "not data errors. The means are valid but skewed by rare extreme events."
        )
    elif real_frac >= 0.60:
        lines.append(
            f"**PARTIALLY** — {real_frac:.1%} are confirmed real moves but a material fraction "
            "are unconfirmed. Use the clean means for thesis evaluation."
        )
    else:
        lines.append(
            f"**NO** — only {real_frac:.1%} are confirmed real moves. More than 40% appear to be "
            "data errors. The original means are unreliable; use clean means exclusively."
        )

    SUMMARY_MD.write_text("\n".join(lines), encoding="utf-8")
    log.info("Summary written: %s", SUMMARY_MD)

    # --- Console output ---
    print()
    print("=" * 60)
    print("EXTREME RETURN AUDIT")
    print("=" * 60)
    print(f"Total extreme rows: {total}")
    for cls, cnt in counts.items():
        print(f"  {cls}: {cnt} ({cnt/total:.1%})")
    print(f"\n{real_frac:.1%} confirmed real moves, {1-real_frac:.1%} artifacts/unconfirmed")
    print(f"\nRecomputed Analysis 3 means (artifacts excluded):")
    print(f"  Top decile × delay > 0.3 : {thesis_mean:+.1f} bps  (was +55.2)")
    print(f"  Top decile (all delay)    : {top_dec_mean:+.1f} bps  (was +30.5)")
    print(f"  Bottom decile             : {bot_dec_mean:+.1f} bps  (was +17.3)")
    print()


if __name__ == "__main__":
    main()
