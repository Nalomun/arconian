#!/usr/bin/env python3
"""
Read-only research check: per-day cross-sectional IC of the backtest scores.

Input : backtest_2024-01-01_2026-04-21.csv (built from licensed Norgate data and is not distributed)
Output: analysis/daily_ic_check.md  plus per-day series CSVs under analysis/

Steps
  1. Verify CSV byte count (137,121,407).
  2. Per-day Spearman(score, fwd_return_3d) for each score; time-series
     mean, sd, share positive, and Newey-West t-stat (lags 3 and 5).
  3. Same within delay buckets A–E (same boundaries as analyze_backtest.py).
  4. Per-day top-decile minus bottom-decile mean return (deciles by
     composite within each day); time-series mean and NW t-stat.
  5. Pooled Spearman, then pooled Spearman after subtracting each day's
     cross-sectional mean return (and, as a second variant, after also
     demeaning the score by day).

Nothing here writes to the DB or touches production code.
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSV = os.path.join(ROOT, "backtest_2024-01-01_2026-04-21.csv")
OUT_DIR = os.path.join(ROOT, "analysis")
OUT_MD = os.path.join(OUT_DIR, "daily_ic_check.md")
EXPECTED_BYTES = 137_121_407
SCORES = ["composite", "volume", "return_mag", "sector_rs", "delay"]
MIN_N_PER_DAY = 20          # skip a (day, bucket) cell with fewer stocks than this
NW_LAGS = (3, 5)

_lines: list[str] = []


def out(s: str = "") -> None:
    print(s)
    _lines.append(s)


# ---------------------------------------------------------------------------
# Newey-West
# ---------------------------------------------------------------------------
def newey_west_t(x: np.ndarray, lags: int) -> tuple[float, float, float]:
    """Mean, NW standard error of the mean, t-stat. Bartlett kernel."""
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    T = len(x)
    if T < 2:
        return np.nan, np.nan, np.nan
    m = x.mean()
    e = x - m
    lrv = np.dot(e, e) / T
    for L in range(1, lags + 1):
        if L >= T:
            break
        w = 1.0 - L / (lags + 1.0)
        lrv += 2.0 * w * np.dot(e[L:], e[:-L]) / T
    se = np.sqrt(lrv / T)
    return m, se, (m / se if se > 0 else np.nan)


def naive_t(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    return x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))


# ---------------------------------------------------------------------------
# Per-group Spearman, vectorised via ranks + groupby
# ---------------------------------------------------------------------------
def per_group_spearman(df: pd.DataFrame, keys: list[str], x: str, y: str,
                       min_n: int) -> pd.DataFrame:
    """Spearman(x, y) within each group defined by `keys`.

    Rows with NaN in x or y are dropped first. Ties get average ranks (same
    as scipy.stats.spearmanr). Returns columns: keys..., n, rho.
    """
    sub = df[keys + [x, y]].dropna(subset=[x, y])
    g = sub.groupby(keys, sort=True, observed=True)
    rx = g[x].rank(method="average")
    ry = g[y].rank(method="average")
    tmp = pd.DataFrame({"rx": rx, "ry": ry})
    for k in keys:
        tmp[k] = sub[k].values
    g2 = tmp.groupby(keys, sort=True, observed=True)
    n = g2.size().rename("n")
    mx = g2["rx"].mean()
    my = g2["ry"].mean()
    sxy = g2.apply(lambda d: ((d["rx"] - d["rx"].mean()) * (d["ry"] - d["ry"].mean())).sum(),
                   include_groups=False)
    sxx = g2.apply(lambda d: ((d["rx"] - d["rx"].mean()) ** 2).sum(), include_groups=False)
    syy = g2.apply(lambda d: ((d["ry"] - d["ry"].mean()) ** 2).sum(), include_groups=False)
    rho = sxy / np.sqrt(sxx * syy)
    res = pd.concat([n, rho.rename("rho")], axis=1).reset_index()
    res.loc[res["n"] < min_n, "rho"] = np.nan
    return res


def summarize_series(name: str, s: pd.Series, extra: str = "") -> dict:
    s = s.dropna()
    row = {
        "series": name,
        "days": len(s),
        "mean": s.mean(),
        "sd": s.std(ddof=1),
        "pos_share": (s > 0).mean(),
        "t_naive": naive_t(s.values),
    }
    for L in NW_LAGS:
        _, se, t = newey_west_t(s.values, L)
        row[f"t_nw{L}"] = t
    row["extra"] = extra
    return row


def fmt_table(rows: list[dict], value_scale: float = 1.0, value_label: str = "mean") -> None:
    hdr = (f"| series | days | {value_label} | sd | % days > 0 | t (naive) | "
           + " | ".join(f"t (NW {L})" for L in NW_LAGS) + " |")
    out(hdr)
    out("|" + "---|" * (6 + len(NW_LAGS)))
    for r in rows:
        cells = [
            r["series"], f"{r['days']}",
            f"{r['mean'] * value_scale:+.4f}" if value_scale == 1 else f"{r['mean'] * value_scale:+.1f}",
            f"{r['sd'] * value_scale:.4f}" if value_scale == 1 else f"{r['sd'] * value_scale:.1f}",
            f"{100 * r['pos_share']:.0f}%",
            f"{r['t_naive']:+.2f}",
        ] + [f"{r[f't_nw{L}']:+.2f}" for L in NW_LAGS]
        out("| " + " | ".join(cells) + " |")


# ---------------------------------------------------------------------------
def bucket(d: float) -> str:
    if pd.isna(d):
        return "E"
    if d > 0.5:
        return "A"
    if d > 0.3:
        return "B"
    if d > 0.1:
        return "C"
    return "D"


BUCKET_LABEL = {
    "A": "A: delay > 0.5",
    "B": "B: 0.3 < delay <= 0.5",
    "C": "C: 0.1 < delay <= 0.3",
    "D": "D: delay <= 0.1",
    "E": "E: delay NULL",
}


def main() -> None:
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)

    out("# Per-day cross-sectional IC check of the backtest CSV")
    out()
    out(f"Run: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}  ·  script: scripts/research/daily_ic_check.py")
    out()

    # ---- Step 1 -----------------------------------------------------------
    out("## 1. Input")
    out()
    nbytes = os.path.getsize(CSV)
    ok = nbytes == EXPECTED_BYTES
    out(f"- File: `{os.path.basename(CSV)}` (built from licensed Norgate data and is not distributed)")
    out(f"- Byte count: {nbytes:,} (expected {EXPECTED_BYTES:,}) — {'MATCH' if ok else 'MISMATCH'}")
    if not ok:
        out("- **Aborting: byte count does not match.**")
        sys.exit(1)

    df = pd.read_csv(CSV, parse_dates=["date"])
    df["bkt"] = df["delay"].map(bucket)
    n_days = df["date"].nunique()
    out(f"- Rows: {len(df):,}  ·  tickers: {df['ticker'].nunique():,}  ·  trading days: {n_days}  "
        f"·  {df['date'].min().date()} → {df['date'].max().date()}")
    out(f"- fwd_return_3d non-null: {df['fwd_return_3d'].notna().sum():,}  ·  delay non-null: {df['delay'].notna().sum():,}")
    out(f"- Stocks per day: min {df.groupby('date').size().min():,}, median {int(df.groupby('date').size().median()):,}, max {df.groupby('date').size().max():,}")
    out()
    out("Newey-West t-stats use a Bartlett kernel; the primary lag is 3 (3-day forward windows overlap by 2 days), "
        "lag 5 is shown as a robustness check. The naive t-stat (iid days) is shown for comparison only.")
    out()

    # ---- Step 2 -----------------------------------------------------------
    out("## 2. Per-day Spearman IC, all stocks")
    out()
    out(f"Each day: Spearman rank correlation across stocks between the score and fwd_return_3d. "
        f"Days with fewer than {MIN_N_PER_DAY} scored stocks are skipped (relevant only for `delay`, which is NULL for the first months).")
    out()
    daily_all: dict[str, pd.Series] = {}
    rows = []
    for sc in SCORES:
        r = per_group_spearman(df, ["date"], sc, "fwd_return_3d", MIN_N_PER_DAY)
        s = r.set_index("date")["rho"]
        daily_all[sc] = s
        rows.append(summarize_series(sc, s))
    fmt_table(rows, value_label="mean IC")
    out()
    daily_df = pd.DataFrame(daily_all)
    daily_df.to_csv(os.path.join(OUT_DIR, "daily_ic_by_score.csv"), float_format="%.6f")
    out(f"Per-day series written to `analysis/daily_ic_by_score.csv`.")
    out()
    # autocorrelation of the daily IC series (justifies the NW lag choice)
    out("Autocorrelation of the daily composite IC series (lags 1–5): "
        + ", ".join(f"{daily_df['composite'].autocorr(L):+.3f}" for L in range(1, 6)))
    out()
    # by calendar third, for the composite
    thirds = pd.qcut(np.arange(len(daily_df)), 3, labels=["early", "middle", "late"])
    out("Composite per-day IC by thirds of the date range (by row count of days):")
    out()
    out("| third | dates | days | mean IC | % days > 0 | t (NW 3) |")
    out("|---|---|---|---|---|---|")
    for lab in ["early", "middle", "late"]:
        s = daily_df["composite"][np.asarray(thirds == lab)]
        m, se, t = newey_west_t(s.values, 3)
        out(f"| {lab} | {s.index.min().date()} → {s.index.max().date()} | {len(s)} | {m:+.4f} | {100*(s>0).mean():.0f}% | {t:+.2f} |")
    out()

    # ---- Step 3 -----------------------------------------------------------
    out("## 3. Per-day Spearman IC within delay buckets")
    out()
    out(f"Same statistic, computed within (day, bucket). A (day, bucket) cell with fewer than {MIN_N_PER_DAY} stocks is skipped; "
        f"`days` is the number of cells that survive. Bucket membership is per row, so a stock's bucket can change day to day.")
    out()
    cell_counts = df.groupby(["date", "bkt"], observed=True).size().unstack(fill_value=0)
    out("Stocks per (day, bucket): median across days")
    out()
    out("| bucket | days with >= " + str(MIN_N_PER_DAY) + " stocks | median stocks/day | first day with >= " + str(MIN_N_PER_DAY) + " |")
    out("|---|---|---|---|")
    for b in "ABCDE":
        c = cell_counts[b] if b in cell_counts else pd.Series(0, index=cell_counts.index)
        okd = c[c >= MIN_N_PER_DAY]
        first = okd.index.min().date() if len(okd) else "—"
        out(f"| {BUCKET_LABEL[b]} | {len(okd)} | {int(c[c>0].median()) if (c>0).any() else 0:,} | {first} |")
    out()
    bucket_series: dict[str, pd.DataFrame] = {}
    for b in "ABCDE":
        sub = df[df["bkt"] == b]
        out(f"### Bucket {BUCKET_LABEL[b]}  (rows: {len(sub):,})")
        out()
        rows = []
        cols = {}
        for sc in SCORES:
            if b == "E" and sc == "delay":
                continue
            r = per_group_spearman(sub, ["date"], sc, "fwd_return_3d", MIN_N_PER_DAY)
            s = r.set_index("date")["rho"]
            cols[sc] = s
            rows.append(summarize_series(sc, s))
        fmt_table(rows, value_label="mean IC")
        out()
        bucket_series[b] = pd.DataFrame(cols)
        bucket_series[b].to_csv(os.path.join(OUT_DIR, f"daily_ic_bucket_{b}.csv"), float_format="%.6f")

    # ---- Step 4 -----------------------------------------------------------
    out("## 4. Per-day decile spread (top decile minus bottom decile, by composite)")
    out()
    out("Each day, stocks are split into ten equal-count bins by composite rank; the spread is the mean fwd_return_3d "
        "of the top bin minus the mean of the bottom bin. Values in bps. The same is shown for the other scores.")
    out()
    valid = df.dropna(subset=["fwd_return_3d"]).copy()
    spread_rows = []
    spread_series = {}
    for sc in SCORES:
        v = valid.dropna(subset=[sc]).copy()
        g = v.groupby("date")
        pct = g[sc].rank(method="first", pct=True)   # 'first' breaks ties so bins are equal-count
        v["dec"] = np.minimum((pct * 10).astype(int) + 1, 10)  # 1..10
        cnt = g.size()
        keep_days = cnt[cnt >= 10 * MIN_N_PER_DAY].index
        v = v[v["date"].isin(keep_days)]
        m = v.groupby(["date", "dec"])["fwd_return_3d"].mean().unstack()
        spread = (m[10] - m[1]) * 1e4
        spread_series[sc] = spread
        spread_rows.append(summarize_series(sc, spread))
        if sc == "composite":
            top_mean = m[10] * 1e4
            bot_mean = m[1] * 1e4
            dec_means = m.mean() * 1e4
    fmt_table(spread_rows, value_scale=1.0, value_label="mean spread (bps)")
    out()
    out("Composite only, additional detail:")
    out()
    mt, set_, tt = newey_west_t(top_mean.values, 3)
    mb, seb, tb = newey_west_t(bot_mean.values, 3)
    out(f"- Time-series mean of daily top-decile mean return: {mt:+.1f} bps (NW3 t = {tt:+.2f})")
    out(f"- Time-series mean of daily bottom-decile mean return: {mb:+.1f} bps (NW3 t = {tb:+.2f})")
    out(f"- Median daily spread: {spread_series['composite'].median():+.1f} bps; "
        f"p10 / p90 of daily spread: {spread_series['composite'].quantile(0.1):+.1f} / {spread_series['composite'].quantile(0.9):+.1f} bps")
    out("- Mean return by within-day decile (time-series average of the daily decile means, bps): "
        + ", ".join(f"D{int(d)} {v:+.1f}" for d, v in dec_means.items()))
    out()
    # Thesis subset: per-day composite decile spread among stocks with delay > 0.3 (buckets A+B)
    out("Thesis subset (delay > 0.3, buckets A+B). Deciles by composite formed each day *within* that subset:")
    out()
    th = valid[valid["delay"] > 0.3].copy()
    g = th.groupby("date")
    pct = g["composite"].rank(method="first", pct=True)
    th["dec"] = np.minimum((pct * 10).astype(int) + 1, 10)
    cnt = g.size()
    th = th[th["date"].isin(cnt[cnt >= 10 * MIN_N_PER_DAY].index)]
    m = th.groupby(["date", "dec"])["fwd_return_3d"].mean().unstack()
    sp = (m[10] - m[1]) * 1e4
    rest = th[th["dec"] < 10].groupby("date")["fwd_return_3d"].mean()
    excess = (m[10] - rest) * 1e4
    fmt_table([summarize_series("D10 − D1, within delay > 0.3", sp),
               summarize_series("D10 − mean of D1..D9, within delay > 0.3", excess)],
              value_label="mean spread (bps)")
    out()
    mt, _, tt = newey_west_t((m[10] * 1e4).values, 3)
    out(f"- Time-series mean of the daily top-decile mean return within delay > 0.3: {mt:+.1f} bps (NW3 t = {tt:+.2f}); "
        f"the pooled version of this cell is the +55.2 bps in analysis_2026-04-21.md (row 30 of PROJECT_HISTORY).")
    out()
    pd.DataFrame(spread_series).to_csv(os.path.join(OUT_DIR, "daily_decile_spread.csv"), float_format="%.3f")
    out("Per-day spreads written to `analysis/daily_decile_spread.csv`.")
    out()
    # Pooled decile spread for reference (the number in analysis_2026-04-21.md)
    pooled_dec = pd.qcut(valid["composite"], 10, labels=False)
    pm = valid.groupby(pooled_dec)["fwd_return_3d"].mean() * 1e4
    out(f"For reference, the pooled (all rows, not per-day) composite D10 − D1 spread is {pm.iloc[-1] - pm.iloc[0]:+.1f} bps, "
        f"matching the +13.2 bps in analysis_2026-04-21.md.")
    out()

    # ---- Step 5 -----------------------------------------------------------
    out("## 5. Why is the pooled IC positive when the per-day IC is about zero?")
    out()
    out("Pooled Spearman over all rows, then the same after removing each day's cross-sectional mean return "
        "(so that only within-day variation in returns remains), then after also demeaning the score by day.")
    out()
    out("| score | n | pooled IC (raw) | pooled IC, return demeaned by day | pooled IC, both demeaned by day | mean per-day IC (from §2) |")
    out("|---|---|---|---|---|---|")
    day_mean_ret = valid.groupby("date")["fwd_return_3d"].transform("mean")
    valid["ret_dm"] = valid["fwd_return_3d"] - day_mean_ret
    pooled_results = {}
    for sc in SCORES:
        v = valid.dropna(subset=[sc])
        raw = spearmanr(v[sc], v["fwd_return_3d"]).statistic
        dm = spearmanr(v[sc], v["ret_dm"]).statistic
        sc_dm = v[sc] - v.groupby("date")[sc].transform("mean")
        both = spearmanr(sc_dm, v["ret_dm"]).statistic
        pooled_results[sc] = (len(v), raw, dm, both)
        out(f"| {sc} | {len(v):,} | {raw:+.4f} | {dm:+.4f} | {both:+.4f} | {daily_all[sc].mean():+.4f} |")
    out()

    # Between-day component: do day-level means of score and return co-move?
    out("Between-day component. Each day's cross-sectional mean score against that day's cross-sectional mean return "
        "(one point per trading day):")
    out()
    out("| score | days | Spearman(day-mean score, day-mean return) | Pearson | NW3 t of Pearson slope proxy |")
    out("|---|---|---|---|---|")
    dm_tab = valid.groupby("date").agg(ret=("fwd_return_3d", "mean"), **{sc: (sc, "mean") for sc in SCORES})
    for sc in SCORES:
        d = dm_tab.dropna(subset=[sc])
        rs = spearmanr(d[sc], d["ret"]).statistic
        rp = np.corrcoef(d[sc], d["ret"])[0, 1]
        # t-stat on the product of standardised day-means (a NW-robust proxy for the correlation)
        zx = (d[sc] - d[sc].mean()) / d[sc].std()
        zy = (d["ret"] - d["ret"].mean()) / d["ret"].std()
        _, _, tz = newey_west_t((zx * zy).values, 3)
        out(f"| {sc} | {len(d)} | {rs:+.3f} | {rp:+.3f} | {tz:+.2f} |")
    out()

    # Exact decomposition of the pooled Spearman in rank space.
    # Spearman = Pearson on pooled ranks. cov(Rx,Ry) = between-day cov + within-day cov exactly.
    out("Exact decomposition of the pooled Spearman. Spearman is the Pearson correlation of the pooled ranks. "
        "The covariance of the pooled ranks splits exactly into a between-day part (covariance of the day means of the ranks) "
        "and a within-day part (covariance of the rank deviations from their day means). Each part is divided by the same "
        "product of pooled rank standard deviations, so the two columns sum to the pooled IC.")
    out()
    out("| score | pooled IC | between-day part | within-day part | corr of day-mean ranks (row-weighted) | between-day share of rank variance: score / return |")
    out("|---|---|---|---|---|---|")
    for sc in SCORES:
        v = valid.dropna(subset=[sc])
        rx = v[sc].rank(method="average")
        ry = v["fwd_return_3d"].rank(method="average")
        gx = rx.groupby(v["date"]).transform("mean")
        gy = ry.groupby(v["date"]).transform("mean")
        denom = rx.std(ddof=0) * ry.std(ddof=0)
        cov_b = ((gx - rx.mean()) * (gy - ry.mean())).mean()
        cov_w = ((rx - gx) * (ry - gy)).mean()
        corr_b = cov_b / (gx.std(ddof=0) * gy.std(ddof=0))
        out(f"| {sc} | {(cov_b + cov_w) / denom:+.4f} | {cov_b / denom:+.4f} | {cov_w / denom:+.4f} | {corr_b:+.3f} | "
            f"{100 * gx.var(ddof=0) / rx.var(ddof=0):.1f}% / {100 * gy.var(ddof=0) / ry.var(ddof=0):.1f}% |")
    out()
    out("Note the between-day share of the *rank* variance of returns is much larger than the between-day share of the raw "
        "return variance below: ranking compresses the fat tails, so day-to-day shifts in the whole cross-section move the ranks a lot.")
    out()

    # Variance decomposition of the return
    tot_var = valid["fwd_return_3d"].var()
    between = day_mean_ret.var()
    out(f"Share of fwd_return_3d variance that is between-day (variance of day means / total variance): "
        f"{100*between/tot_var:.1f}%. Share of composite variance that is between-day: "
        f"{100*valid.groupby('date')['composite'].transform('mean').var()/valid['composite'].var():.1f}%.")
    out()

    # How much of the day-mean return variation is the market? Use the equal-weight day-mean itself as the "market".
    # Also: correlation of day-mean composite with day-mean *past* activity is baked into volume percentile by design.
    out("Time-series of daily cross-sectional mean return and mean composite, by calendar quarter:")
    out()
    q = valid.groupby(valid["date"].dt.to_period("Q")).agg(
        days=("date", "nunique"), rows=("date", "size"),
        mean_ret_bps=("fwd_return_3d", lambda x: 1e4 * x.mean()),
        mean_composite=("composite", "mean"), mean_volume=("volume", "mean"),
        mean_retmag=("return_mag", "mean"),
    )
    out("| quarter | days | rows | mean fwd 3d return (bps) | mean composite | mean volume score | mean return_mag score |")
    out("|---|---|---|---|---|---|---|")
    for idx, r in q.iterrows():
        out(f"| {idx} | {int(r['days'])} | {int(r['rows']):,} | {r['mean_ret_bps']:+.1f} | {r['mean_composite']:.4f} | {r['mean_volume']:.4f} | {r['mean_retmag']:.4f} |")
    out()

    out(f"_Runtime {time.time() - t0:.0f}s._")

    with open(OUT_MD, "w") as fh:
        fh.write("\n".join(_lines) + "\n")
    print(f"\nWrote {OUT_MD}")


if __name__ == "__main__":
    main()
