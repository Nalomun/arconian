"""
Arconian v3 — Stratified IC analysis on backtest results.
Read-only analysis: does not modify backtest.py, database, or any signal code.

Reads:  backtest_2024-01-01_2026-04-21.csv  (project root)
Writes: analysis_2026-04-21.md              (project root)
"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

# ---------------------------------------------------------------------------
# Constants (whitepaper references)
# ---------------------------------------------------------------------------
IC_THRESHOLD  = 0.02     # §7.4 — flag threshold
COST_BPS      = 120.0    # §5 — round-trip cost assumption
DELAY_PRIMARY = 0.3      # §2.3 — primary target lower bound

PROJECT_ROOT = Path(__file__).parent.parent
CSV_PATH     = PROJECT_ROOT / "backtest_2024-01-01_2026-04-21.csv"
REPORT_PATH  = PROJECT_ROOT / "analysis_2026-04-21.md"

EXPECTED_COLS = {
    "date", "ticker", "composite", "volume",
    "return_mag", "sector_rs", "delay", "fwd_return_3d",
}

# ---------------------------------------------------------------------------
# Dual-output: stdout + buffer (buffer becomes the markdown body)
# ---------------------------------------------------------------------------
_buf = StringIO()


def _out(text: str = "") -> None:
    print(text)
    _buf.write(text + "\n")


# Key findings collected during analysis; used to generate headline + next steps.
_findings: dict = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bps(x) -> float:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return float("nan")
    return round(float(x) * 10_000, 1)


def _ic_pair(a: pd.Series, b: pd.Series) -> tuple[float, float]:
    valid = pd.concat([a.rename("a"), b.rename("b")], axis=1).dropna()
    if len(valid) < 10:
        return float("nan"), float("nan")
    ic, pval = spearmanr(valid["a"], valid["b"])
    return round(float(ic), 4), round(float(pval), 4)


def _pct_row(series: pd.Series) -> str:
    arr = series.dropna().values.astype(float)
    if len(arr) == 0:
        return "N/A"
    vals = np.nanpercentile(arr, [5, 25, 50, 75, 95])
    return "  ".join(f"p{p}={v:.4f}" for p, v in zip([5, 25, 50, 75, 95], vals))


def _assign_deciles(series: pd.Series) -> pd.Series:
    """Assign 1-based decile labels; returns NaN where qcut can't assign."""
    try:
        d = pd.qcut(series, q=10, labels=False, duplicates="drop")
        return d + 1
    except Exception:
        return pd.Series(np.nan, index=series.index)


def _top_decile_index(series: pd.Series) -> pd.Index:
    """Return index labels of observations in the top decile of series."""
    d = _assign_deciles(series)
    max_d = d.max()
    if pd.isna(max_d):
        return pd.Index([])
    return d[d == max_d].index


# ---------------------------------------------------------------------------
# Load & validate
# ---------------------------------------------------------------------------

def load_data() -> pd.DataFrame:
    if not CSV_PATH.exists():
        sys.exit(f"ERROR: CSV not found at {CSV_PATH}")

    dtype_map = {
        "composite":     "float32",
        "volume":        "float32",
        "return_mag":    "float32",
        "sector_rs":     "float32",
        "delay":         "float32",
        "fwd_return_3d": "float32",
    }
    df = pd.read_csv(CSV_PATH, dtype=dtype_map, parse_dates=["date"],
                     low_memory=False)

    missing = EXPECTED_COLS - set(df.columns)
    if missing:
        sys.exit(f"ERROR: Schema mismatch — missing columns: {missing}")

    return df


# ---------------------------------------------------------------------------
# Analysis 5: Sanity Checks
# ---------------------------------------------------------------------------

def analysis_sanity(df: pd.DataFrame) -> None:
    _out("## Analysis 5: Sanity Checks")
    _out()

    total          = len(df)
    n_comp         = int(df["composite"].notna().sum())
    n_delay        = int(df["delay"].notna().sum())
    pct_delay_null = df["delay"].isna().mean() * 100
    n_tickers      = df["ticker"].nunique()
    date_min       = df["date"].min().date()
    date_max       = df["date"].max().date()

    _out(f"Total rows:                   {total:>12,}")
    _out(f"Rows with non-null composite: {n_comp:>12,}")
    _out(f"Rows with non-null delay:     {n_delay:>12,}  ({100 - pct_delay_null:.1f}% populated)")
    _out(f"Unique tickers:               {n_tickers:>12,}")
    _out(f"Date range:                   {date_min} → {date_max}")
    _out()

    _out("Composite score distribution (expect roughly 0.0–1.0):")
    _out("  " + _pct_row(df["composite"]))
    _out()

    _out("Delay score distribution (non-null rows):")
    _out("  " + _pct_row(df["delay"].dropna()))
    _out(f"  % null: {pct_delay_null:.1f}%")
    _out()

    fwd = df["fwd_return_3d"].dropna().astype(float)
    extreme_hi = int((fwd > 1.0).sum())
    extreme_lo = int((fwd < -0.9).sum())
    if extreme_hi > 0 or extreme_lo > 0:
        _out(f"[WARNING] Extreme forward returns: {extreme_hi:,} > +100%,  "
             f"{extreme_lo:,} < -90% — may indicate unadjusted corporate actions")
    else:
        _out("No extreme forward returns detected (all within ±90%).")

    c_min, c_max = float(df["composite"].min()), float(df["composite"].max())
    if c_min < 0 or c_max > 1:
        _out(f"[WARNING] Composite outside [0,1]: min={c_min:.4f}, max={c_max:.4f}")
    else:
        _out(f"Composite range: [{c_min:.4f}, {c_max:.4f}] — OK")

    _out()


# ---------------------------------------------------------------------------
# Analysis 1: Composite Score Decile Lift
# ---------------------------------------------------------------------------

def analysis_decile_lift(df: pd.DataFrame) -> pd.Index:
    """Returns the index labels of the top-composite-decile observations."""
    _out("## Analysis 1: Composite Score Decile Lift")
    _out()

    valid = df.dropna(subset=["composite", "fwd_return_3d"]).copy()
    valid["decile"] = _assign_deciles(valid["composite"])
    valid = valid.dropna(subset=["decile"])

    rows = []
    for d in sorted(valid["decile"].unique()):
        sub = valid[valid["decile"] == d]["fwd_return_3d"].astype(float)
        n   = len(sub)
        if n == 0:
            continue
        rows.append({
            "Decile":       int(d),
            "N":            n,
            "Mean (bps)":   _bps(sub.mean()),
            "Median (bps)": _bps(sub.median()),
            "Hit Rate (%)": round((sub > 0).mean() * 100, 1),
            "SE (bps)":     _bps(sub.std() / np.sqrt(n)),
        })

    dec_df = pd.DataFrame(rows).set_index("Decile")
    _out(dec_df.to_string())
    _out()

    top_row     = dec_df.iloc[-1]
    bot_row     = dec_df.iloc[0]
    ls_spread   = round(top_row["Mean (bps)"] - bot_row["Mean (bps)"], 1)
    top_mean    = top_row["Mean (bps)"]

    _out(f"Long-short spread (D10 − D1):  {ls_spread:+.1f} bps")
    cost_word = "CLEARS" if top_mean >= COST_BPS else "below"
    _out(f"Top-decile mean return:        {top_mean:+.1f} bps  "
         f"({cost_word} §5 cost threshold of {COST_BPS:.0f} bps)")

    means       = dec_df["Mean (bps)"].values
    dec_labels  = dec_df.index.values.astype(float)
    mono_r, mono_p = spearmanr(dec_labels, means)
    mono_r, mono_p = round(float(mono_r), 4), round(float(mono_p), 4)
    _out(f"Monotonicity (Spearman r):     {mono_r:+.4f}  p={mono_p:.4f}")

    if mono_r > 0.7:
        interp = "Strong monotonic lift — composite decile reliably tracks forward returns."
    elif mono_r > 0.3:
        interp = "Moderate monotonic lift — composite has directional signal but noisy."
    else:
        interp = "Weak or absent monotonic lift — composite decile does not reliably track returns."
    _out(f"Interpretation: {interp}")
    _out()

    _findings["A1_ls_spread"]        = ls_spread
    _findings["A1_top_mean_bps"]     = top_mean
    _findings["A1_mono_r"]           = mono_r
    _findings["A1_top_clears_cost"]  = bool(top_mean >= COST_BPS)

    return valid[valid["decile"] == valid["decile"].max()].index


# ---------------------------------------------------------------------------
# Analysis 2: IC Segmented by Delay Bucket
# ---------------------------------------------------------------------------

def analysis_delay_buckets(df: pd.DataFrame) -> None:
    _out("## Analysis 2: IC Segmented by Delay Bucket")
    _out()
    _out("Buckets (per §2.3):")
    _out("  A: delay > 0.5           (very high delay)")
    _out("  B: 0.3 < delay ≤ 0.5     (high delay — primary targets)")
    _out("  C: 0.1 < delay ≤ 0.3     (low delay)")
    _out("  D: delay ≤ 0.1           (very low delay)")
    _out("  E: delay = NULL           (insufficient history)")
    _out()

    def _bucket(d):
        if pd.isna(d):
            return "E"
        d = float(d)
        if d > 0.5:  return "A"
        if d > 0.3:  return "B"
        if d > 0.1:  return "C"
        return "D"

    df = df.copy()
    df["_bkt"] = df["delay"].map(_bucket)

    signal_cols = ["composite", "volume", "return_mag", "sector_rs"]
    bucket_labels = {
        "A": "Bucket A: delay > 0.5",
        "B": "Bucket B: 0.3 < delay ≤ 0.5",
        "C": "Bucket C: 0.1 < delay ≤ 0.3",
        "D": "Bucket D: delay ≤ 0.1",
        "E": "Bucket E: delay = NULL",
    }

    key_ics: dict[str, float] = {}

    for bkt in ["A", "B", "C", "D", "E"]:
        sub  = df[df["_bkt"] == bkt].dropna(subset=["fwd_return_3d"])
        n    = len(sub)
        _out(f"--- {bucket_labels[bkt]}  (n={n:,}) ---")

        if n < 30:
            _out("  [SKIP] Fewer than 30 valid-fwd-return observations — IC unreliable")
            _out()
            continue

        for sig in signal_cols:
            ic_val, pval = _ic_pair(sub[sig], sub["fwd_return_3d"])
            if np.isnan(ic_val):
                _out(f"  {sig:<12}  IC=N/A  (insufficient non-null pairs)")
                continue
            flag = "  *** CLEARS §7.4" if ic_val >= IC_THRESHOLD else ""
            _out(f"  {sig:<12}  IC={ic_val:+.4f}  p={pval:.4f}{flag}")
            if sig == "composite":
                key_ics[bkt] = ic_val

        # Top composite decile within this bucket
        comp_valid = sub["composite"].dropna()
        if len(comp_valid) >= 20:
            try:
                top_idx_within = _top_decile_index(comp_valid)
                top_fwd = sub.loc[sub.index.intersection(top_idx_within),
                                  "fwd_return_3d"].dropna().astype(float)
                n_top   = len(top_fwd)
                top_m   = _bps(top_fwd.mean()) if n_top > 0 else float("nan")
                _out(f"  Top-decile within bucket: n={n_top:,}  mean={top_m:+.1f} bps")
            except Exception as exc:
                _out(f"  [WARNING] Top-decile within bucket failed: {exc}")

        if n < 500:
            _out(f"  [NOTE] n={n:,} is small — treat IC estimates with caution")

        _out()

    _out("Critical question (does composite IC in primary target buckets clear §7.4?):")
    for bkt in ["A", "B"]:
        sub_n = len(df[df["_bkt"] == bkt].dropna(subset=["composite", "fwd_return_3d"]))
        ic_val = key_ics.get(bkt)
        if ic_val is not None and not np.isnan(ic_val):
            clears = ic_val >= IC_THRESHOLD
            _out(f"  {bucket_labels[bkt]}: composite IC = {ic_val:+.4f}  "
                 f"({'CLEARS §7.4' if clears else 'does NOT clear §7.4'})  n={sub_n:,}")
        else:
            _out(f"  {bucket_labels[bkt]}: insufficient data for IC (n={sub_n:,})")
    _out()

    _findings["A2_bucket_ics"]    = key_ics
    _findings["A2_thesis_lives"]  = bool(
        key_ics.get("A", float("nan")) >= IC_THRESHOLD
        or key_ics.get("B", float("nan")) >= IC_THRESHOLD
    )


# ---------------------------------------------------------------------------
# Analysis 3: Joint Stratification — Top Decile × High Delay
# ---------------------------------------------------------------------------

def analysis_joint(df: pd.DataFrame, top_idx: pd.Index) -> None:
    _out("## Analysis 3: Joint Stratification — Top Decile × Delay > 0.3")
    _out()

    top_mask        = pd.Series(df.index.isin(top_idx), index=df.index)
    high_delay_mask = (df["delay"] > DELAY_PRIMARY).fillna(False)

    subsets = [
        ("Top decile × delay > 0.3  (thesis target)",
         df[top_mask & high_delay_mask]),
        ("Top decile, all delay",
         df[top_mask]),
        ("delay > 0.3, all deciles",
         df[high_delay_mask]),
    ]

    for label, sub in subsets:
        sub = sub.dropna(subset=["fwd_return_3d"])
        n   = len(sub)
        _out(f"--- {label}  (n={n:,}) ---")

        if n < 10:
            _out("  [SKIP] Fewer than 10 observations")
            _out()
            continue

        fwd = sub["fwd_return_3d"].astype(float)
        mean_bps   = _bps(fwd.mean())
        med_bps    = _bps(fwd.median())
        hit_pct    = round((fwd > 0).mean() * 100, 1)
        se_bps     = _bps(fwd.std() / np.sqrt(n))
        after_cost = round(mean_bps - COST_BPS, 1)

        pct_vals = np.nanpercentile(fwd.values, [5, 25, 50, 75, 95])
        pct_str  = "  ".join(
            f"p{p}={_bps(v):+.1f}" for p, v in zip([5, 25, 50, 75, 95], pct_vals)
        )

        _out(f"  Mean fwd return:              {mean_bps:+.1f} bps")
        _out(f"  Median fwd return:            {med_bps:+.1f} bps")
        _out(f"  Hit rate:                     {hit_pct:.1f}%")
        _out(f"  Standard error:               {se_bps:.1f} bps")
        _out(f"  After cost (mean − {COST_BPS:.0f} bps):  {after_cost:+.1f} bps  "
             f"({'positive — may cover costs' if after_cost > 0 else 'negative — does not cover costs'})")
        _out(f"  Percentiles:                  {pct_str}")
        _out()

        if "thesis target" in label:
            _findings["A3_thesis_mean_bps"]    = mean_bps
            _findings["A3_thesis_after_cost"]  = after_cost
            _findings["A3_thesis_n"]           = n
            _findings["A3_thesis_clears_cost"] = bool(after_cost > 0)


# ---------------------------------------------------------------------------
# Analysis 4: Time Stability
# ---------------------------------------------------------------------------

def analysis_time_stability(df: pd.DataFrame, top_idx: pd.Index) -> None:
    _out("## Analysis 4: Time Stability")
    _out()

    sorted_dates = df["date"].sort_values()
    n_total  = len(sorted_dates)
    cut1     = sorted_dates.iloc[n_total // 3]
    cut2     = sorted_dates.iloc[2 * n_total // 3]

    thirds = [
        ("Early",  df[df["date"] <= cut1]),
        ("Middle", df[(df["date"] > cut1) & (df["date"] <= cut2)]),
        ("Late",   df[df["date"] > cut2]),
    ]

    top_mask        = pd.Series(df.index.isin(top_idx), index=df.index)
    high_delay_mask = (df["delay"] > DELAY_PRIMARY).fillna(False)

    _out(f"{'Period':<8}  {'Date range':<30}  {'Comp IC':>10}  "
         f"{'Top-dec mean':>14}  {'Top×Delay mean':>16}")
    _out("-" * 84)

    period_data = []
    for period, sub in thirds:
        dr_str = f"{sub['date'].min().date()} → {sub['date'].max().date()}"

        valid    = sub.dropna(subset=["composite", "fwd_return_3d"])
        ic_val, _ = _ic_pair(valid["composite"], valid["fwd_return_3d"])

        top_sub  = sub[top_mask[sub.index]]
        top_fwd  = top_sub.dropna(subset=["fwd_return_3d"])["fwd_return_3d"].astype(float)
        top_mean = _bps(top_fwd.mean()) if len(top_fwd) > 0 else float("nan")

        td_hd_sub  = sub[top_mask[sub.index] & high_delay_mask[sub.index]]
        td_hd_fwd  = td_hd_sub.dropna(subset=["fwd_return_3d"])["fwd_return_3d"].astype(float)
        td_hd_mean = _bps(td_hd_fwd.mean()) if len(td_hd_fwd) > 0 else float("nan")

        ic_s      = f"{ic_val:+.4f}" if not np.isnan(ic_val) else "    N/A"
        top_s     = f"{top_mean:+.1f}"  if not np.isnan(top_mean)  else "N/A"
        td_hd_s   = f"{td_hd_mean:+.1f}" if not np.isnan(td_hd_mean) else "N/A"

        _out(f"{period:<8}  {dr_str:<30}  {ic_s:>10}  {top_s:>14}  {td_hd_s:>16}")
        period_data.append((period, ic_val, top_mean, td_hd_mean))

    _out()

    ics = [r[1] for r in period_data if not np.isnan(r[1])]
    if len(ics) >= 2:
        if ics[0] > ics[-1] + 0.005:
            ic_trend = "Declining — composite IC falling through the backtest window (see §1.4 lifespan note)"
        elif ics[-1] > ics[0] + 0.005:
            ic_trend = "Rising — composite IC improving through the backtest window"
        else:
            ic_trend = "Stable — composite IC roughly flat across time periods"
        _out(f"IC trend:         {ic_trend}")

    td_means = [r[3] for r in period_data if not np.isnan(r[3])]
    if len(td_means) >= 2:
        if td_means[0] > td_means[-1] + 5:
            td_trend = "Declining — thesis subset return falling over time"
        elif td_means[-1] > td_means[0] + 5:
            td_trend = "Rising — thesis subset return improving over time"
        else:
            td_trend = "Stable — thesis subset return roughly flat over time"
        _out(f"Top×Delay trend:  {td_trend}")

    _out()

    _findings["A4_ic_trend"]    = ics
    _findings["A4_td_hd_trend"] = td_means
    _findings["A4_is_stable"]   = bool(len(ics) >= 2 and abs(ics[-1] - ics[0]) < 0.01)


# ---------------------------------------------------------------------------
# Markdown-only sections
# ---------------------------------------------------------------------------

def _headline_section() -> str:
    f    = _findings
    ls   = f.get("A1_ls_spread", float("nan"))
    mono = f.get("A1_mono_r", float("nan"))
    top  = f.get("A1_top_mean_bps", float("nan"))

    lines = ["## Headline Findings\n"]

    # 1. Decile lift
    if not np.isnan(ls):
        qual = "strong" if abs(ls) > 100 else ("moderate" if abs(ls) > 30 else "weak or absent")
        lines.append(
            f"- **Decile lift ({qual}):** long-short spread = {ls:+.1f} bps, "
            f"monotonicity Spearman r = {mono:+.4f}. "
            f"Top-decile mean = {top:+.1f} bps "
            f"({'clears' if f.get('A1_top_clears_cost') else 'does not clear'} §5 cost threshold)."
        )

    # 2. Delay-bucket IC
    bkt_ics = f.get("A2_bucket_ics", {})
    ic_a    = bkt_ics.get("A", float("nan"))
    ic_b    = bkt_ics.get("B", float("nan"))
    lives   = f.get("A2_thesis_lives", False)
    ic_a_s  = f"{ic_a:+.4f}" if not np.isnan(ic_a) else "N/A"
    ic_b_s  = f"{ic_b:+.4f}" if not np.isnan(ic_b) else "N/A"
    lines.append(
        f"- **Delay-bucket composite IC:** Bucket A = {ic_a_s}, Bucket B = {ic_b_s}. "
        f"Thesis {'lives — IC clears §7.4 in at least one high-delay bucket' if lives else 'does NOT live — no high-delay bucket clears §7.4'}."
    )

    # 3. Joint top-decile × high-delay
    ac  = f.get("A3_thesis_after_cost", float("nan"))
    n3  = f.get("A3_thesis_n", 0)
    m3  = f.get("A3_thesis_mean_bps", float("nan"))
    if not np.isnan(ac):
        lines.append(
            f"- **Top decile × delay > 0.3 (n={n3:,}):** mean = {m3:+.1f} bps, "
            f"after-cost = {ac:+.1f} bps "
            f"({'positive' if ac > 0 else 'negative'} vs §5 threshold)."
        )

    # 4. Time stability
    ics    = f.get("A4_ic_trend", [])
    stable = f.get("A4_is_stable", False)
    if len(ics) >= 2:
        trend_word = "stable" if stable else ("declining" if ics[0] > ics[-1] else "rising")
        lines.append(
            f"- **Time stability:** composite IC {trend_word} across backtest window "
            f"({min(ics):+.4f} to {max(ics):+.4f}). "
            f"{'No evidence of §1.4 decay within this window.' if stable else 'See §1.4 — signal may be in decay.'}"
        )

    lines.append("")
    return "\n".join(lines)


def _next_steps_section() -> str:
    f      = _findings
    ac     = f.get("A3_thesis_after_cost", float("nan"))
    stable = f.get("A4_is_stable", False)
    lives  = f.get("A2_thesis_lives", False)
    mono   = f.get("A1_mono_r", 0.0)

    lines = ["## What This Means for Next Steps\n"]
    lines.append(
        "The go/no-go decision for Phase 1 paper trading (§11) is not made here. "
        "This section maps combinations of findings to their strategic implications.\n"
    )

    if not np.isnan(ac):
        if ac > 40 and stable:
            lines.append(
                "**After-cost positive AND time-stable** (current case): "
                "the composite has measurable edge on its thesis subset. "
                "The remaining gap vs. the live system (options signal, retail attention penalty, "
                "earnings proximity filter, spread/OI filters — see backtest.py header and §2/§3) "
                "is material and could move the IC in either direction. "
                "If Analysis 2 shows thesis_lives=True, the delay filter is load-bearing and "
                "should be enforced in the Phase 1 entry logic. "
                "Rebuilding the backtest with earnings exclusion and directional confirmation "
                "before Phase 1 would reduce go-live uncertainty; "
                "alternatively, begin Phase 1 with small size and treat those layers as in-flight work."
            )
        elif ac > 0:
            lines.append(
                "**After-cost return is positive but modest:** there is directional signal, "
                "but the margin is narrow. "
                "The missing backtest layers (earnings exclusion, directional confirmation) "
                "could shift this materially. "
                "Recommended: rebuild with those layers before Phase 1, unless time pressure "
                "favors paper-trading now at reduced size."
            )
        else:
            lines.append(
                "**After-cost return is negative:** the thesis subset does not cover transaction "
                "costs in the current backtest form. "
                "This does not necessarily kill the thesis — the missing layers (options signal, "
                "retail penalty, earnings exclusion) could flip the sign. "
                "If Analysis 2 shows composite IC clears §7.4 in Buckets A/B, "
                "the composite has information content that is being eaten by friction; "
                "the priority should be rebuilding with earnings/directional layers, not proceeding to Phase 1."
            )
        lines.append("")

    if not lives and mono < 0.3:
        lines.append(
            "**Both Analysis 1 (monotonicity) and Analysis 2 (delay-bucket IC) are weak:** "
            "consider whether the backtest limitations (options neutral, no retail penalty) "
            "are responsible, or whether signal weightings need IC-based recalibration (§7.4 recalibration "
            "is a live-system feature deferred from the backtest). "
            "A weak aggregate result that is also weak in high-delay subsets suggests "
            "either a design issue or a market regime not captured in 2024–2026."
        )
        lines.append("")

    if not stable and len(f.get("A4_ic_trend", [])) >= 2:
        lines.append(
            "**Signal decay detected (Analysis 4):** if IC is declining toward the end of the "
            "backtest window, verify the pattern is real before Phase 1. "
            "A signal strong in early 2024 but dead by late 2025 should not drive live capital. "
            "Check whether the decay correlates with a macro regime change (rate environment, "
            "small-cap volatility) that may reverse."
        )
        lines.append("")

    lines.append(
        "**General note:** the backtest operates without options signal, retail attention penalty, "
        "earnings proximity exclusion, and exact spread/OI filters — all present in the live system. "
        "Backtest IC is a lower bound on live IC only if those layers add independent information. "
        "If they remove noise (earnings volatility, illiquid names), live IC could be higher."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _out("=" * 72)
    _out("ARCONIAN V3 — STRATIFIED IC ANALYSIS")
    _out("Backtest file: backtest_2024-01-01_2026-04-21.csv")
    _out("Date: 2026-04-21")
    _out("=" * 72)
    _out()

    df = load_data()

    analysis_sanity(df)
    top_idx = analysis_decile_lift(df)
    analysis_delay_buckets(df)
    analysis_joint(df, top_idx)
    analysis_time_stability(df, top_idx)

    headline   = _headline_section()
    body       = _buf.getvalue()
    next_steps = _next_steps_section()

    md_header = (
        "# Arconian v3 — Stratified IC Analysis\n\n"
        f"**Backtest file:** `backtest_2024-01-01_2026-04-21.csv`  \n"
        f"**Generated:** 2026-04-21  \n\n"
    )
    report = md_header + headline + "\n---\n\n" + body + "\n---\n\n" + next_steps
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"\nReport written to: {REPORT_PATH}")


if __name__ == "__main__":
    main()
