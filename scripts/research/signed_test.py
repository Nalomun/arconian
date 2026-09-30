#!/usr/bin/env python3
"""
Read-only research check: per-day IC of the backtest scores against the SIGNED
3-day forward return, sign(day-t return) x fwd_return_3d.

Pre-registration: analysis/signed_test_preregistration.md (commit daf7354 in the
private development repository).
Method is daily_ic_check.py's (its helpers are imported, not re-implemented);
only the return definition changes.

Input : backtest_2024-01-01_2026-04-21.csv            (licensed Norgate data; not distributed)
        crsp_pull/day_t_returns.parquet               (crsp_pull_day_t_returns.py)
Output: analysis/signed_test.md, analysis/signed_daily_ic.csv,
        analysis/signed_decile_spread.csv, analysis/signed_daily_topdecile.csv

Nothing here writes to the DB or touches production code.
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daily_ic_check import (  # noqa: E402  same method, same code
    EXPECTED_BYTES, MIN_N_PER_DAY, NW_LAGS, bucket, naive_t, newey_west_t, per_group_spearman,
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSV = os.path.join(ROOT, "backtest_2024-01-01_2026-04-21.csv")
CRSP = os.path.join(ROOT, "crsp_pull", "day_t_returns.parquet")
OUT_DIR = os.path.join(ROOT, "analysis")
OUT_MD = os.path.join(OUT_DIR, "signed_test.md")
SCORES = ["composite", "volume", "return_mag", "sector_rs", "delay"]
COST_LONG_BPS = 120.0
COST_SHORT_BPS = 150.0
EXPECTED_PRIMARY_ROWS = 952_002      # pre-registration §2
EXPECTED_PRIMARY_DAYS = 288

_lines: list[str] = []


def out(s: str = "") -> None:
    print(s)
    _lines.append(s)


# ---------------------------------------------------------------------------
def summarize(name: str, s: pd.Series) -> dict:
    s = s.dropna()
    row = {"series": name, "days": len(s), "mean": s.mean(), "median": s.median(),
           "sd": s.std(ddof=1), "pos": (s > 0).mean(), "t_naive": naive_t(s.values)}
    for L in NW_LAGS:
        row[f"t_nw{L}"] = newey_west_t(s.values, L)[2]
    return row


def table(rows: list[dict], bps: bool = False) -> None:
    f = (lambda v: f"{v:+.1f}") if bps else (lambda v: f"{v:+.4f}")
    g = (lambda v: f"{v:.1f}") if bps else (lambda v: f"{v:.4f}")
    unit = " (bps)" if bps else ""
    out(f"| series | days | mean{unit} | median{unit} | sd | % days > 0 | t (naive) | "
        + " | ".join(f"t (NW {L})" for L in NW_LAGS) + " |")
    out("|" + "---|" * (7 + len(NW_LAGS)))
    for r in rows:
        out("| " + " | ".join([r["series"], str(r["days"]), f(r["mean"]), f(r["median"]), g(r["sd"]),
                               f"{100 * r['pos']:.0f}%", f"{r['t_naive']:+.2f}"]
                              + [f"{r[f't_nw{L}']:+.2f}" for L in NW_LAGS]) + " |")


def daily_ic(df: pd.DataFrame, score: str, ret: str) -> pd.Series:
    return per_group_spearman(df, ["date"], score, ret, MIN_N_PER_DAY).set_index("date")["rho"]


def deciles(df: pd.DataFrame, score: str) -> pd.DataFrame:
    """daily_ic_check.py's within-day decile rule; drops days with < 10*MIN_N_PER_DAY stocks."""
    v = df.dropna(subset=[score]).copy()
    g = v.groupby("date")
    pct = g[score].rank(method="first", pct=True)
    v["dec"] = np.minimum((pct * 10).astype(int) + 1, 10)
    cnt = g.size()
    return v[v["date"].isin(cnt[cnt >= 10 * MIN_N_PER_DAY].index)]


def spreads(v: pd.DataFrame, ret: str) -> tuple[pd.Series, pd.Series]:
    """(D10 - D1, D10 - mean(D1..D9)) per day, in bps. v already has 'dec'."""
    m = v.groupby(["date", "dec"])[ret].mean().unstack()
    rest = v[v["dec"] < 10].groupby("date")[ret].mean()
    return (m[10] - m[1]) * 1e4, (m[10] - rest) * 1e4


def top_net(v: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Daily top-decile mean of gross and net signed return (bps). v already has 'dec'."""
    top = v[v["dec"] == 10]
    return top.groupby("date")["sfwd"].mean() * 1e4, top.groupby("date")["net"].mean() * 1e4


def decomposition(v: pd.DataFrame, sc: str, ret: str) -> tuple[float, float, float, float]:
    v = v.dropna(subset=[sc])
    rx = v[sc].rank(method="average")
    ry = v[ret].rank(method="average")
    gx = rx.groupby(v["date"]).transform("mean")
    gy = ry.groupby(v["date"]).transform("mean")
    denom = rx.std(ddof=0) * ry.std(ddof=0)
    cov_b = ((gx - rx.mean()) * (gy - ry.mean())).mean()
    cov_w = ((rx - gx) * (ry - gy)).mean()
    dm = v[ret] - v.groupby("date")[ret].transform("mean")
    return (cov_b + cov_w) / denom, cov_b / denom, cov_w / denom, spearmanr(v[sc], dm).statistic


def build_sample(m: pd.DataFrame, sign_col: str) -> pd.DataFrame:
    s = m[(m["map_status"] == "ok") & (m[sign_col] != 0)].copy()
    s["dir"] = np.sign(s[sign_col]).astype(int)
    s["sfwd"] = s["dir"] * s["fwd_return_3d"]
    s["net"] = s["sfwd"] - np.where(s["dir"] > 0, COST_LONG_BPS, COST_SHORT_BPS) / 1e4
    s["bkt"] = s["delay"].map(bucket)
    return s


def primary(s: pd.DataFrame) -> dict:
    ic = daily_ic(s, "composite", "sfwd")
    ic_raw = daily_ic(s, "composite", "fwd_return_3d")
    v = deciles(s, "composite")
    gross, net = top_net(v)
    return {"ic": ic, "ic_raw": ic_raw, "gross": gross, "net": net,
            "t": newey_west_t(ic.values, 3)[2], "net_mean": net.mean()}


def verdict(p: dict) -> str:
    if p["t"] >= 2.0 and p["net_mean"] > 0:
        return "alive"
    if p["t"] <= -2.0:
        return "reversal"
    if p["t"] >= 2.0:
        return "dead: not tradeable"
    return "dead"


# ---------------------------------------------------------------------------
def main() -> None:
    t0 = time.time()
    out("# Signed-return per-day IC test")
    out()
    out(f"Run: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}  ·  script: scripts/research/signed_test.py  ·  "
        "pre-registration: analysis/signed_test_preregistration.md (commit daf7354 in the private development repository)")
    out()

    # ---- Input --------------------------------------------------------------
    nbytes = os.path.getsize(CSV)
    if nbytes != EXPECTED_BYTES:
        sys.exit(f"CSV byte count {nbytes} != {EXPECTED_BYTES}")
    bt = pd.read_csv(CSV, parse_dates=["date"])
    cr = pd.read_parquet(CRSP, columns=["date", "ticker", "permno", "ret_t", "retx_t", "map_status"])
    m = bt.merge(cr, on=["date", "ticker"], how="left", validate="one_to_one")
    assert len(m) == len(bt)

    s = build_sample(m, "retx_t")
    s_rob = build_sample(m, "ret_t")
    ok = m[m["map_status"] == "ok"]
    out("## 1. Sample")
    out()
    out(f"- CSV: {len(bt):,} rows, {bt['date'].nunique()} days (byte count matches {EXPECTED_BYTES:,}).")
    out(f"- Matched (`map_status == ok`): {len(ok):,} rows, {ok['date'].nunique()} days, "
        f"{ok['date'].min().date()} → {ok['date'].max().date()}.")
    out(f"- **Primary sample (sign = sign(DLYRETX)):** {len(s):,} rows, {s['date'].nunique()} days; "
        f"dropped {int((ok['retx_t'] == 0).sum()):,} rows with DLYRETX = 0.")
    out(f"- Robustness sample (sign = sign(DLYRET)): {len(s_rob):,} rows; "
        f"dropped {int((ok['ret_t'] == 0).sum()):,} rows with DLYRET = 0.")
    out(f"- Direction mix, primary sample: long (DLYRETX > 0) {100 * (s['dir'] > 0).mean():.1f}%, "
        f"short {100 * (s['dir'] < 0).mean():.1f}%.")
    out(f"- Stocks per day: min {s.groupby('date').size().min():,}, median {int(s.groupby('date').size().median()):,}.")
    if len(s) != EXPECTED_PRIMARY_ROWS or s["date"].nunique() != EXPECTED_PRIMARY_DAYS:
        out("- **Primary sample does not match the pre-registered counts; aborting.**")
        sys.exit(1)
    out()
    out("Signed forward return `sfwd = sign(day-t return) × fwd_return_3d`. Net = sfwd − 120 bps (long) or − 150 bps (short). "
        "NW t: Bartlett kernel, 3 lags decide, 5 lags for reference.")
    out()

    # ---- Primary ------------------------------------------------------------
    p = primary(s)
    v_ = verdict(p)
    out("## 2. Primary test (confirmatory)")
    out()
    table([summarize("composite IC vs signed fwd (PRIMARY)", p["ic"]),
           summarize("composite IC vs raw fwd, same rows (control)", p["ic_raw"])])
    out()
    out("Top composite decile, within day, direction-appropriate costs (bps):")
    out()
    table([summarize("top-decile gross signed return", p["gross"]),
           summarize("top-decile NET signed return (decision)", p["net"])], bps=True)
    out()
    out(f"Decision rule: alive iff primary NW3 t ≥ +2.0 and mean daily top-decile net > 0; reversal iff t ≤ −2.0; else dead.")
    out(f"- Primary NW3 t = {p['t']:+.2f}; mean daily top-decile net = {p['net_mean']:+.1f} bps.")
    out(f"- **Verdict: {v_}**")
    out()
    pr = primary(s_rob)
    out("Robustness, sign(DLYRET) (no decision weight):")
    out()
    table([summarize("composite IC vs signed fwd, sign(DLYRET)", pr["ic"])])
    out()
    table([summarize("top-decile NET signed return, sign(DLYRET)", pr["net"])], bps=True)
    out(f"- Verdict under the same rule with sign(DLYRET): {verdict(pr)}")
    out()
    out("Autocorrelation of the daily primary IC series (lags 1–5): "
        + ", ".join(f"{p['ic'].autocorr(L):+.3f}" for L in range(1, 6)))
    out()

    # ---- Secondary: scores ---------------------------------------------------
    out("## 3. EXPLORATORY — per-day IC by score, signed vs raw (same rows)")
    out()
    ic_series = {}
    rows = []
    for sc in SCORES:
        a = daily_ic(s, sc, "sfwd")
        b = daily_ic(s, sc, "fwd_return_3d")
        ic_series[f"{sc}_signed"] = a
        ic_series[f"{sc}_raw"] = b
        rows += [summarize(f"{sc} — signed", a), summarize(f"{sc} — raw", b)]
    table(rows)
    out()
    pd.DataFrame(ic_series).to_csv(os.path.join(OUT_DIR, "signed_daily_ic.csv"), float_format="%.6f")

    # ---- Secondary: decile spreads --------------------------------------------
    out("## 4. EXPLORATORY — within-day decile signed spreads (bps)")
    out()
    rows = []
    spread_series = {}
    for sc in SCORES:
        v = deciles(s, sc)
        d1, dr = spreads(v, "sfwd")
        spread_series[f"{sc}_d10_d1"] = d1
        spread_series[f"{sc}_d10_rest"] = dr
        rows += [summarize(f"{sc}: D10 − D1", d1), summarize(f"{sc}: D10 − rest", dr)]
    vc = deciles(s, "composite")
    r1, rr = spreads(vc, "fwd_return_3d")
    rows += [summarize("composite: D10 − D1, RAW (control)", r1), summarize("composite: D10 − rest, RAW (control)", rr)]
    table(rows, bps=True)
    out()
    dm = vc.groupby(["date", "dec"])["sfwd"].mean().unstack().mean() * 1e4
    out("Composite, mean signed return by within-day decile (time-series average of daily decile means, bps): "
        + ", ".join(f"D{int(d)} {x:+.1f}" for d, x in dm.items()))
    out()
    pd.DataFrame(spread_series).to_csv(os.path.join(OUT_DIR, "signed_decile_spread.csv"), float_format="%.3f")

    # ---- Cost breakdown -------------------------------------------------------
    out("## 5. EXPLORATORY — top composite decile by trade direction (bps)")
    out()
    top = vc[vc["dec"] == 10]
    out(f"Top-decile direction mix: long {100 * (top['dir'] > 0).mean():.1f}%, short {100 * (top['dir'] < 0).mean():.1f}% "
        f"(whole sample: long {100 * (s['dir'] > 0).mean():.1f}%).")
    out()
    rows = []
    for lab, sub in [("long", top[top["dir"] > 0]), ("short", top[top["dir"] < 0])]:
        rows.append(summarize(f"top decile {lab}: gross signed", sub.groupby("date")["sfwd"].mean() * 1e4))
        rows.append(summarize(f"top decile {lab}: net", sub.groupby("date")["net"].mean() * 1e4))
    table(rows, bps=True)
    out()
    pd.DataFrame({"gross_bps": p["gross"], "net_bps": p["net"]}).to_csv(
        os.path.join(OUT_DIR, "signed_daily_topdecile.csv"), float_format="%.3f")

    # ---- Buckets + thesis subset ----------------------------------------------
    out("## 6. EXPLORATORY — delay buckets and thesis subset (delay > 0.3)")
    out()
    cells = [(f"bucket {b}", s[s["bkt"] == b]) for b in "ABCDE"] + [("thesis (delay > 0.3)", s[s["delay"] > 0.3])]
    reversal_cells = []
    for lab, sub in cells:
        out(f"### {lab}  (rows {len(sub):,})")
        out()
        rows = []
        for sc in SCORES:
            if lab == "bucket E" and sc == "delay":
                continue
            a = daily_ic(sub, sc, "sfwd")
            b = daily_ic(sub, sc, "fwd_return_3d")
            ra, rb = summarize(f"{sc} IC — signed", a), summarize(f"{sc} IC — raw", b)
            rows += [ra, rb]
            if ra["t_nw3"] <= -2.0:
                reversal_cells.append(f"{lab}, {sc} signed IC: mean {ra['mean']:+.4f}, NW3 t {ra['t_nw3']:+.2f}")
        table(rows)
        out()
        v = deciles(sub, "composite")
        if v["date"].nunique() == 0:
            out(f"No day has ≥ {10 * MIN_N_PER_DAY} stocks in this cell; decile statistics skipped.")
            out()
            continue
        d1, dr = spreads(v, "sfwd")
        g, n = top_net(v)
        table([summarize("composite D10 − D1 signed", d1), summarize("composite D10 − rest signed", dr),
               summarize("top-decile gross signed", g), summarize("top-decile NET", n)], bps=True)
        out()
    if len(reversal_cells) == 0:
        out("Secondary cells with a signed IC at NW3 t ≤ −2.0 (all scores, all cells): none.")
    else:
        out("Secondary cells with a signed IC at NW3 t ≤ −2.0 (exploratory):")
        for c in reversal_cells:
            out(f"- {c}")
    # also the whole-sample score ICs
    whole = [f"all stocks, {k.replace('_signed', '')}: NW3 t {newey_west_t(v.values, 3)[2]:+.2f}"
             for k, v in ic_series.items() if k.endswith("_signed") and newey_west_t(v.values, 3)[2] <= -2.0]
    out("Whole-sample signed ICs at NW3 t ≤ −2.0: " + ("; ".join(whole) if whole else "none") + ".")
    out()

    # ---- Decomposition -------------------------------------------------------
    out("## 7. EXPLORATORY — between-day / within-day decomposition of the pooled signed IC")
    out()
    out("| score | pooled signed IC | between-day part | within-day part | pooled IC, sfwd demeaned by day | mean per-day signed IC |")
    out("|---|---|---|---|---|---|")
    for sc in SCORES:
        pooled, b, w, dmd = decomposition(s, sc, "sfwd")
        out(f"| {sc} | {pooled:+.4f} | {b:+.4f} | {w:+.4f} | {dmd:+.4f} | {ic_series[sc + '_signed'].mean():+.4f} |")
    out()
    pooled, b, w, dmd = decomposition(s, "composite", "fwd_return_3d")
    out(f"Control, composite vs raw fwd on the same rows: pooled {pooled:+.4f} = between {b:+.4f} + within {w:+.4f}.")
    out()

    # ---- Matched vs unmatched --------------------------------------------------
    out("## 8. Matched vs unmatched rows (descriptive, no returns)")
    out()
    last_day = s["date"].max()
    full = m.copy()
    g = full.groupby("date")
    pct = g["composite"].rank(method="first", pct=True)
    full["top_dec"] = np.minimum((pct * 10).astype(int) + 1, 10) == 10
    in_win = full["date"] <= last_day
    zero = (full["map_status"] == "ok") & (full["retx_t"] == 0)
    groups = [
        ("used in primary sample", in_win & (full["map_status"] == "ok") & ~zero),
        ("matched, DLYRETX = 0 (dropped)", in_win & zero),
        ("no PERMNO", in_win & (full["map_status"] == "no_permno")),
        ("identity check failed", in_win & (full["map_status"] == "fwd_mismatch")),
        ("PERMNO but no CRSP row", in_win & (full["map_status"] == "no_crsp_row_or_ret")),
        ("excluded period 2025-12-29 → 2026-04-15", ~in_win),
    ]
    out("| group | rows | composite mean | median | p10 / p90 | % in within-day top decile | % delay non-NULL | delay mean | delay median |")
    out("|---|---|---|---|---|---|---|---|---|")
    for lab, mask in groups:
        x = full[mask]
        dl = x["delay"].dropna()
        out(f"| {lab} | {len(x):,} | {x['composite'].mean():.4f} | {x['composite'].median():.4f} | "
            f"{x['composite'].quantile(0.1):.3f} / {x['composite'].quantile(0.9):.3f} | {100 * x['top_dec'].mean():.1f}% | "
            f"{100 * x['delay'].notna().mean():.1f}% | {dl.mean():.3f} | {dl.median():.3f} |")
    out()
    out("Top-decile membership uses deciles formed on all CSV rows of each day. A share of 10% means no over/under-representation.")
    out()
    out(f"_Runtime {time.time() - t0:.0f}s._")
    with open(OUT_MD, "w") as fh:
        fh.write("\n".join(_lines) + "\n")
    print(f"\nWrote {OUT_MD}")


if __name__ == "__main__":
    main()
