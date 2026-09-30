#!/usr/bin/env python3
"""
Pull the signed day-t return for every (date, ticker) row of the backtest CSV
from CRSP (CIZ / "v2" format) via WRDS, and validate the ticker -> PERMNO
mapping against the CSV's own fwd_return_3d.

Why: the backtest CSV has no signed day-t return (return_mag is a percentile of
|return|). The signed test needs sign(close(t-1) -> close(t)). The backtest
used Norgate total-return-adjusted closes, whose day-over-day ratio is a total
return, so the CRSP analogue is DLYRET (with distributions). DLYRETX (ex
distributions) is pulled too, for reference only.

Run it yourself (needs a WRDS account with CRSP daily stock access):

    pip install wrds pyarrow
    python scripts/research/crsp_pull_day_t_returns.py --wrds-user YOUR_WRDS_USERNAME

Input : backtest_2024-01-01_2026-04-21.csv at the repo root
        (built from licensed Norgate data and is not distributed)
Output: crsp_pull/  (CRSP-licensed data; do NOT commit)
    day_t_returns.parquet   one row per CSV row: date, ticker, permno, ret_t,
                            retx_t, prc_flag, prev_dt, ret_miss_flag,
                            fwd3_crsp, fwd3_csv, fwd3_absdiff, fwd3_match,
                            map_status
    ticker_map.csv          per Norgate ticker: candidate PERMNOs, chosen
                            PERMNO, match rate
    pull_report.txt         the summary printed to stdout

Nothing here touches production code, config, or the database.

Mapping method
  1. Norgate symbol -> candidate CRSP symbols (strip Norgate's "-YYYYMM"
     delisted suffix; for class shares "BRK.B" also try "BRKB", "BRK/B",
     "BRK B", and root "BRK" + shareclass "B").
  2. Candidate PERMNOs = any crsp.stksecurityinfohist record whose ticker or
     tradingsymbol equals a candidate and whose validity interval overlaps
     2024-11-01 .. 2026-04-21. (Norgate symbols are as of April 2026; matching
     on any overlapping interval catches names that changed ticker mid-window.)
  3. For each candidate PERMNO, recompute the 3-day forward total return from
     DLYRET on the CRSP trading calendar (the backtest used the SPY calendar,
     which is the same NYSE calendar) and compare to fwd_return_3d.
  4. Per ticker, choose the candidate with the highest share of rows matching
     within FWD_TOL. A row is kept for the signed test only if its own
     forward-return check passes (fwd3_match == True).
The forward-return comparison is used ONLY to validate identity. The day-t
return itself comes straight from DLYRET on date t; nothing is inferred from
forward returns.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSV = os.path.join(ROOT, "backtest_2024-01-01_2026-04-21.csv")
OUT_DIR = os.path.join(ROOT, "crsp_pull")
EXPECTED_BYTES = 137_121_407

WINDOW_START = "2024-11-01"     # first signal date in the CSV
WINDOW_END = "2026-04-21"       # last signal date 2026-04-15 + 3 trading days (+ buffer)
PULL_START = "2024-10-25"       # small buffer before the first signal date
PULL_END = "2026-04-24"
FORWARD_DAYS = 3
FWD_TOL = 5e-4                  # |fwd3_crsp - fwd3_csv| <= 5 bps counts as a match
PERMNO_CHUNK = 500

SECINFO = ("crsp", "stksecurityinfohist")
DAILY = ("crsp", "stkdlysecuritydata")
SECINFO_REQUIRED = ["permno", "secinfostartdt", "secinfoenddt", "ticker", "tradingsymbol"]
DAILY_REQUIRED = ["permno", "dlycaldt", "dlyret", "dlyretx", "dlyprc"]
DAILY_OPTIONAL = ["dlyprcflg", "dlyprevdt", "dlyretmissflg"]

_report: list[str] = []


def out(s: str = "") -> None:
    print(s)
    _report.append(s)


# ---------------------------------------------------------------------------
# Symbol handling
# ---------------------------------------------------------------------------
def candidate_symbols(sym: str) -> tuple[set[str], tuple[str, str] | None]:
    """Norgate symbol -> (set of CRSP ticker/tradingsymbol candidates, (root, class) or None)."""
    base = re.sub(r"-\d{6}$", "", sym)          # Norgate delisted suffix, e.g. HOLX-202604
    cands = {base}
    root_cls = None
    if "." in base:
        root, cls = base.split(".", 1)
        cands |= {root + cls, f"{root}/{cls}", f"{root} {cls}", f"{root}.{cls}"}
        root_cls = (root, cls)
    if re.search(r"-[A-Z]+$", base):             # Norgate preferred form, e.g. BAC-L
        root, cls = base.rsplit("-", 1)
        cands |= {f"{root}P{cls}", f"{root}.PR{cls}", f"{root}/P{cls}"}
    return cands, root_cls


# ---------------------------------------------------------------------------
# WRDS helpers
# ---------------------------------------------------------------------------
def check_columns(db, lib: str, table: str, required: list[str], optional: list[str]) -> list[str]:
    desc = db.describe_table(library=lib, table=table)
    cols = [c.lower() for c in desc["name"].tolist()]
    missing = [c for c in required if c not in cols]
    if missing:
        out(f"ERROR: {lib}.{table} is missing required columns {missing}.")
        out(f"Available columns: {cols}")
        out("Your WRDS CRSP schema differs from what this script expects. Send me the column "
            "list above and I'll adjust the queries.")
        sys.exit(2)
    return [c for c in optional if c in cols]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wrds-user", required=True)
    ap.add_argument("--csv", default=CSV)
    ap.add_argument("--out-dir", default=OUT_DIR)
    args = ap.parse_args()

    import wrds  # imported here so --help works without it

    t0 = time.time()
    os.makedirs(args.out_dir, exist_ok=True)

    # ---- 1. The backtest CSV ------------------------------------------------
    nbytes = os.path.getsize(args.csv)
    out(f"CSV: {args.csv}  bytes={nbytes:,}  expected={EXPECTED_BYTES:,}  "
        f"{'MATCH' if nbytes == EXPECTED_BYTES else 'MISMATCH'}")
    if nbytes != EXPECTED_BYTES:
        sys.exit("CSV byte count mismatch; this needs the original backtest CSV (licensed Norgate data, not distributed).")
    bt = pd.read_csv(args.csv, usecols=["date", "ticker", "fwd_return_3d"], parse_dates=["date"])
    out(f"rows={len(bt):,}  tickers={bt.ticker.nunique():,}  "
        f"dates={bt.date.min().date()} -> {bt.date.max().date()} ({bt.date.nunique()} days)")

    # ---- 2. WRDS connection and schema check --------------------------------
    db = wrds.Connection(wrds_username=args.wrds_user)
    check_columns(db, *SECINFO, SECINFO_REQUIRED, ["shareclass", "securitytype", "sharetype",
                                                    "primaryexch", "securitynm"])
    daily_opt = check_columns(db, *DAILY, DAILY_REQUIRED, DAILY_OPTIONAL)

    maxdt = db.raw_sql(f"select max(dlycaldt) as d from {DAILY[0]}.{DAILY[1]}", date_cols=["d"])["d"].iloc[0]
    out(f"CRSP daily data available through {pd.Timestamp(maxdt).date()}")
    if pd.Timestamp(maxdt) < pd.Timestamp(WINDOW_END):
        out(f"WARNING: your CRSP vintage ends before {WINDOW_END}. Rows after "
            f"{pd.Timestamp(maxdt).date()} (and forward windows crossing it) cannot be "
            "matched. Annual-only CRSP subscriptions typically stop at the prior December; "
            "you need the quarterly or monthly update to cover Jan-Apr 2026.")

    # ---- 3. Name history -> candidate PERMNOs --------------------------------
    names = db.raw_sql(
        f"""
        select *
        from {SECINFO[0]}.{SECINFO[1]}
        where secinfoenddt >= '{WINDOW_START}' and secinfostartdt <= '{WINDOW_END}'
        """,
        date_cols=["secinfostartdt", "secinfoenddt"],
    )
    names.columns = [c.lower() for c in names.columns]
    names["permno"] = names["permno"].astype(int)
    for c in ("ticker", "tradingsymbol", "shareclass"):
        if c in names.columns:
            names[c] = names[c].astype("string").str.strip().str.upper()
    out(f"stksecurityinfohist records overlapping window: {len(names):,} "
        f"({names.permno.nunique():,} PERMNOs)")

    by_sym: dict[str, set[int]] = {}
    for col in ("ticker", "tradingsymbol"):
        for s, p in zip(names[col], names["permno"]):
            if pd.notna(s):
                by_sym.setdefault(s, set()).add(p)
    by_root_cls: dict[tuple[str, str], set[int]] = {}
    if "shareclass" in names.columns:
        for r, c, p in zip(names["ticker"], names["shareclass"], names["permno"]):
            if pd.notna(r) and pd.notna(c):
                by_root_cls.setdefault((r, c), set()).add(p)

    cand_map: dict[str, set[int]] = {}
    for sym in bt.ticker.unique():
        cands, root_cls = candidate_symbols(sym)
        permnos: set[int] = set()
        for c in cands:
            permnos |= by_sym.get(c, set())
        if root_cls is not None:
            permnos |= by_root_cls.get(root_cls, set())
        cand_map[sym] = permnos
    n_none = sum(1 for v in cand_map.values() if not v)
    n_multi = sum(1 for v in cand_map.values() if len(v) > 1)
    out(f"tickers with no candidate PERMNO: {n_none:,}; with >1 candidate: {n_multi:,}")

    all_permnos = sorted({p for v in cand_map.values() for p in v})

    # ---- 4. Daily returns for all candidate PERMNOs ---------------------------
    sel = ", ".join(DAILY_REQUIRED + daily_opt)
    date_cols = ["dlycaldt"] + (["dlyprevdt"] if "dlyprevdt" in daily_opt else [])
    frames = []
    for i in range(0, len(all_permnos), PERMNO_CHUNK):
        chunk = ",".join(str(int(p)) for p in all_permnos[i:i + PERMNO_CHUNK])
        frames.append(db.raw_sql(
            f"""
            select {sel}
            from {DAILY[0]}.{DAILY[1]}
            where permno in ({chunk})
              and dlycaldt between '{PULL_START}' and '{PULL_END}'
            """,
            date_cols=date_cols,
        ))
        print(f"  pulled PERMNOs {i + 1}-{min(i + PERMNO_CHUNK, len(all_permnos))} of {len(all_permnos)}")
    db.close()
    daily = pd.concat(frames, ignore_index=True)
    daily.columns = [c.lower() for c in daily.columns]
    daily["permno"] = daily["permno"].astype(int)
    daily = daily.sort_values(["permno", "dlycaldt"]).drop_duplicates(["permno", "dlycaldt"])
    out(f"daily rows pulled: {len(daily):,}")

    # ---- 5. Forward 3-day total return from DLYRET on the CRSP calendar -------
    cal = pd.DatetimeIndex(sorted(daily["dlycaldt"].unique()))
    # Total-return index per PERMNO, reindexed to the calendar with forward fill
    # (mirrors backtest.py's `Close[index <= td]` lookup for days a name did not trade).
    ret = daily.pivot(index="dlycaldt", columns="permno", values="dlyret").reindex(cal)
    tri = (1.0 + ret.fillna(0.0)).cumprod()
    first_valid = ret.notna().cumsum() > 0
    tri = tri.where(first_valid)
    fwd = tri.shift(-FORWARD_DAYS) / tri - 1.0            # close(t) -> close(t+3)
    fwd.index.name = "date"
    fwd_long = (fwd.reset_index()
                .melt(id_vars="date", var_name="permno", value_name="fwd3_crsp")
                .dropna(subset=["fwd3_crsp"]))
    fwd_long["permno"] = fwd_long["permno"].astype(int)

    day = daily.rename(columns={"dlycaldt": "date", "dlyret": "ret_t", "dlyretx": "retx_t",
                                "dlyprcflg": "prc_flag", "dlyprevdt": "prev_dt",
                                "dlyretmissflg": "ret_miss_flag"})
    keep = ["date", "permno", "ret_t", "retx_t", "dlyprc"] + \
        [c for c in ("prc_flag", "prev_dt", "ret_miss_flag") if c in day.columns]
    day = day[keep].merge(fwd_long, on=["date", "permno"], how="left")

    # ---- 6. Validate each (ticker, candidate PERMNO) pair ---------------------
    pairs = pd.DataFrame([(s, p) for s, ps in cand_map.items() for p in ps],
                         columns=["ticker", "permno"])
    cand = bt.merge(pairs, on="ticker", how="inner").merge(day, on=["date", "permno"], how="left")
    cand["fwd3_absdiff"] = (cand["fwd3_crsp"] - cand["fwd_return_3d"]).abs()
    cand["fwd3_match"] = cand["fwd3_absdiff"] <= FWD_TOL

    rate = (cand.groupby(["ticker", "permno"])["fwd3_match"].mean()
            .rename("match_rate").reset_index())
    best = (rate.sort_values(["ticker", "match_rate"], ascending=[True, False])
            .drop_duplicates("ticker"))
    tmap = pd.DataFrame({"ticker": list(cand_map)})
    tmap["candidates"] = tmap["ticker"].map(lambda s: " ".join(str(p) for p in sorted(cand_map[s])))
    tmap = tmap.merge(best, on="ticker", how="left")
    tmap.to_csv(os.path.join(args.out_dir, "ticker_map.csv"), index=False)

    chosen = cand.merge(best[["ticker", "permno"]], on=["ticker", "permno"], how="inner")
    res = bt.merge(chosen.drop(columns=["fwd_return_3d"]), on=["date", "ticker"], how="left")
    res = res.rename(columns={"fwd_return_3d": "fwd3_csv"})
    res["fwd3_match"] = res["fwd3_match"].fillna(False).astype(bool)
    res["map_status"] = np.select(
        [res["permno"].isna(), res["ret_t"].isna(), ~res["fwd3_match"]],
        ["no_permno", "no_crsp_row_or_ret", "fwd_mismatch"],
        default="ok",
    )
    res.to_parquet(os.path.join(args.out_dir, "day_t_returns.parquet"), index=False)

    # ---- 7. Report -----------------------------------------------------------
    out()
    out("Row-level mapping status (rows of the backtest CSV):")
    vc = res["map_status"].value_counts()
    for k in ("ok", "fwd_mismatch", "no_crsp_row_or_ret", "no_permno"):
        n = int(vc.get(k, 0))
        out(f"  {k:<20} {n:>10,}  ({n / len(res):.2%})")
    ok = res[res["map_status"] == "ok"]
    out(f"tickers with >=1 ok row: {ok.ticker.nunique():,} of {res.ticker.nunique():,}")
    d = res["fwd3_absdiff"].dropna()
    if len(d):
        q = d.quantile([0.5, 0.9, 0.99, 0.999]).to_dict()
        out("fwd-return |CRSP - CSV| on mapped rows: "
            + ", ".join(f"p{int(k * 1000) / 10:g}={v:.2e}" for k, v in q.items()))
        out(f"share within 1e-5: {(d <= 1e-5).mean():.2%}; within 1e-4: {(d <= 1e-4).mean():.2%}; "
            f"within tol {FWD_TOL:g}: {(d <= FWD_TOL).mean():.2%}")
    out(f"ok rows with ret_t == 0 exactly: {(ok['ret_t'] == 0).sum():,}")
    out(f"ok rows where sign(ret_t) != sign(retx_t): "
        f"{(np.sign(ok['ret_t']) != np.sign(ok['retx_t'])).sum():,}")
    if "prc_flag" in ok.columns:
        out("ok rows by DLYPRCFLG: " + ", ".join(f"{k}={v:,}" for k, v in
                                                  ok["prc_flag"].value_counts(dropna=False).items()))
    miss_by_cat = res.assign(
        cat=np.select(
            [res.ticker.str.contains(r"-\d{6}$"), res.ticker.str.contains(r"-[A-Z]+$"),
             res.ticker.str.contains(r"\."), res.ticker.str.match(r"^[A-Z]{4}[YF]$")],
            ["norgate_delisted_suffix", "preferred_dash", "class_dot", "5char_Y_or_F"],
            default="plain"),
    ).groupby("cat")["map_status"].apply(lambda s: (s == "ok").mean())
    out("ok share by ticker form: " + ", ".join(f"{k}={v:.1%}" for k, v in miss_by_cat.items()))

    try:
        ign = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", args.out_dir]).returncode == 0
    except Exception:
        ign = False
    if not ign:
        out()
        out(f"NOTE: {args.out_dir} is NOT gitignored. It holds CRSP-licensed data; do not commit it.")
    out(f"_Runtime {time.time() - t0:.0f}s._")
    with open(os.path.join(args.out_dir, "pull_report.txt"), "w") as f:
        f.write("\n".join(_report) + "\n")


if __name__ == "__main__":
    main()
