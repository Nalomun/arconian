"""
Read-only audit of Arconian database state.

Answers: how close are we to the Phase 1 evaluation gate (40 trades),
the IC recalibration window (60 trading days), and the ML logistic
regression prerequisites (700 events) per arconian-v3.md §11 / §8.1?

Opens arconian.db in read-only mode so it can run safely while the
live process holds the writer lock.

Usage: python scripts/survey_state.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "arconian.db"


def _q_one(c, sql, *args):
    r = c.execute(sql, args).fetchone()
    return r[0] if r else None


def _q(c, sql, *args):
    return c.execute(sql, args).fetchall()


def _hr(label: str = "") -> None:
    print()
    if label:
        print(f"---- {label} ----")
    else:
        print("-" * 70)


def main() -> None:
    print("=" * 70)
    print("ARCONIAN STATE SURVEY")
    print(f"DB: {DB_PATH}")
    print("=" * 70)

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    c = conn.cursor()

    # ---- signal_log basics ----
    _hr("signal_log")
    total = _q_one(c, "SELECT COUNT(*) FROM signal_log")
    print(f"Total rows: {total}")
    dr = c.execute(
        "SELECT MIN(scan_timestamp), MAX(scan_timestamp) FROM signal_log"
    ).fetchone()
    print(f"Scan range: {dr[0]}  ->  {dr[1]}")
    distinct_days = _q_one(
        c, "SELECT COUNT(DISTINCT date(scan_timestamp)) FROM signal_log"
    )
    print(f"Distinct scan days: {distinct_days}")

    print()
    print("Rows per scan_type:")
    for st, n in _q(
        c,
        "SELECT scan_type, COUNT(*) FROM signal_log "
        "GROUP BY scan_type ORDER BY 2 DESC",
    ):
        print(f"  {(st or 'NULL'):12s}  {n}")

    # Outcome labels
    labeled = _q_one(c, "SELECT COUNT(*) FROM signal_log WHERE return_3d IS NOT NULL")
    triple = _q_one(
        c, "SELECT COUNT(*) FROM signal_log WHERE outcome_label IS NOT NULL"
    )
    print()
    print(
        f"Labeled (return_3d not null):    {labeled}  "
        f"({labeled / total * 100:.1f}%)"
    )
    print(
        f"Triple-barrier labeled:          {triple}  "
        f"({triple / total * 100:.1f}%)"
    )

    # Composite score distribution (percentile via cumulative)
    print()
    print("Composite score distribution:")
    n_scored = _q_one(
        c, "SELECT COUNT(*) FROM signal_log WHERE composite_score IS NOT NULL"
    )
    if n_scored:
        for label, frac in [("min", 0.0), ("p25", 0.25), ("p50", 0.50),
                            ("p75", 0.75), ("p90", 0.90), ("p95", 0.95),
                            ("p99", 0.99), ("max", 1.0)]:
            offset = max(0, int(n_scored * frac) - 1) if frac < 1.0 else n_scored - 1
            v = _q_one(
                c,
                "SELECT composite_score FROM signal_log "
                "WHERE composite_score IS NOT NULL "
                f"ORDER BY composite_score LIMIT 1 OFFSET {offset}",
            )
            if v is not None:
                print(f"  {label:5s}  {v:.4f}")
        for thresh in [0.50, 0.60, 0.70, 0.80]:
            n = _q_one(
                c,
                "SELECT COUNT(*) FROM signal_log WHERE composite_score >= ?",
                thresh,
            )
            print(f"  >= {thresh:.2f}:  {n}  ({n / total * 100:.1f}%)")

    # Direction
    print()
    print("Direction signal:")
    for d, n in _q(
        c,
        "SELECT direction_signal, COUNT(*) FROM signal_log "
        "GROUP BY direction_signal ORDER BY 2 DESC",
    ):
        print(f"  {(d or 'NULL'):12s}  {n}")

    traded = _q_one(c, "SELECT COUNT(*) FROM signal_log WHERE was_traded = 1")
    print(f"\nwas_traded = 1:  {traded}")

    # Component availability
    print()
    print("Component availability (non-NULL):")
    cols = [
        "volume_pctile_60d",
        "return_pctile_60d",
        "options_composite",
        "sector_rs_percentile",
        "delay_score_as_of_signal",
        "retail_attention_score",
    ]
    for col in cols:
        n = _q_one(c, f"SELECT COUNT({col}) FROM signal_log")
        print(f"  {col:30s}  {n}  ({n / total * 100:.1f}%)")

    # History status
    print()
    print("History status:")
    for hs, n in _q(
        c,
        "SELECT history_status, COUNT(*) FROM signal_log GROUP BY history_status",
    ):
        print(f"  {(hs or 'NULL'):12s}  {n}")

    # ---- trades ----
    _hr("trades")
    total_trades = _q_one(c, "SELECT COUNT(*) FROM trades")
    open_trades = _q_one(c, "SELECT COUNT(*) FROM trades WHERE exit_time IS NULL")
    closed = total_trades - open_trades
    print(f"Total trades: {total_trades}  (open: {open_trades}, closed: {closed})")

    if closed > 0:
        row = c.execute("""
            SELECT
                COUNT(*),
                SUM(realized_pnl),
                AVG(realized_pnl),
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END),
                SUM(CASE WHEN realized_pnl <= 0 THEN -realized_pnl ELSE 0 END)
            FROM trades WHERE exit_time IS NOT NULL
        """).fetchone()
        n, total_pnl, avg_pnl, wins, gp, gl = row
        win_rate = wins / n * 100 if n else 0
        pf = (gp / gl) if gl else float("inf")
        print(f"  Total realized P&L:  ${(total_pnl or 0):.2f}")
        print(f"  Avg P&L per trade:   ${(avg_pnl or 0):.2f}")
        print(f"  Win rate:            {win_rate:.1f}%  ({wins}/{n})")
        print(f"  Profit factor:       {pf:.2f}")

        print("\n  Exit reasons:")
        for er, n2 in _q(
            c,
            "SELECT exit_reason, COUNT(*) FROM trades WHERE exit_time IS NOT NULL "
            "GROUP BY exit_reason ORDER BY 2 DESC",
        ):
            print(f"    {(er or 'NULL'):22s}  {n2}")

    tp1 = _q_one(c, "SELECT COUNT(*) FROM trades WHERE tp1_hit = 1")
    if total_trades:
        print(
            f"\n  TP1 hits across all trades:  {tp1} / {total_trades}  "
            f"({tp1 / total_trades * 100:.1f}%)"
        )

    if open_trades > 0:
        print("\n  Currently-open trades:")
        rows = c.execute("""
            SELECT ticker, entry_time, entry_price, shares, stop_price, tp1_hit
            FROM trades WHERE exit_time IS NULL ORDER BY entry_time
        """).fetchall()
        for r in rows:
            tp1_marker = " (TP1 hit)" if r[5] else ""
            print(
                f"    {r[0]:6s}  entry {r[1]} @ {r[2]:.2f}  "
                f"shares={r[3]}  stop={r[4]:.2f}{tp1_marker}"
            )

    # ---- ic_history ----
    _hr("ic_history")
    ic_n = _q_one(c, "SELECT COUNT(*) FROM ic_history")
    print(f"Total IC computation rows: {ic_n}")
    if ic_n:
        print("Most recent computations:")
        for r in c.execute("""
            SELECT computed_at, signal_component, segment, ic_value, ic_count, decay_flag
            FROM ic_history ORDER BY computed_at DESC LIMIT 12
        """).fetchall():
            print(
                f"  {r[0]}  {r[1]:10s}  segment={r[2]:14s}  "
                f"IC={r[3]}  n={r[4]}  decay={bool(r[5])}"
            )

    # ---- parameter_history ----
    _hr("parameter_history")
    ph_n = _q_one(c, "SELECT COUNT(*) FROM parameter_history")
    print(f"Total parameter changes: {ph_n}")
    if ph_n:
        print("Last 15 changes:")
        for r in c.execute("""
            SELECT timestamp, parameter_path, old_value, new_value, reason, triggered_by
            FROM parameter_history ORDER BY timestamp DESC LIMIT 15
        """).fetchall():
            print(
                f"  {r[0]}  {r[1]:30s}  {r[2]} -> {r[3]}  "
                f"({r[4]}; by {r[5]})"
            )

    # ---- universe_state ----
    _hr("universe_state")
    us_n = _q_one(c, "SELECT COUNT(*) FROM universe_state")
    print(f"Total tickers tracked: {us_n}")
    print("By state:")
    for state, n in _q(
        c,
        "SELECT state, COUNT(*) FROM universe_state GROUP BY state ORDER BY 2 DESC",
    ):
        print(f"  {(state or 'NULL'):15s}  {n}")

    # ---- daily volume last 14 days ----
    _hr("daily signal volume (last 14 days)")
    rows = c.execute("""
        SELECT date(scan_timestamp) AS d, scan_type, COUNT(*)
        FROM signal_log
        WHERE scan_timestamp >= date('now', '-14 days')
        GROUP BY d, scan_type
        ORDER BY d DESC, scan_type
    """).fetchall()
    if rows:
        for d, st, n in rows:
            print(f"  {d}  {st:10s}  {n}")
    else:
        print("  no rows in last 14 days")

    # ---- IC quick estimate (if labeled) ----
    if labeled >= 30:
        _hr("Quick IC estimate (labeled rows, all-time)")
        try:
            from scipy.stats import spearmanr
            import math

            data = c.execute("""
                SELECT volume_pctile_60d, return_pctile_60d, options_composite,
                       sector_rs_percentile, delay_score_as_of_signal,
                       composite_score, return_3d
                FROM signal_log WHERE return_3d IS NOT NULL
            """).fetchall()
            labels = ["volume_60d", "return_60d", "options",
                      "sector_rs", "delay", "composite"]
            for i, name in enumerate(labels):
                pairs = [(r[i], r[6]) for r in data
                         if r[i] is not None and r[6] is not None]
                if len(pairs) >= 10:
                    sig, ret = zip(*pairs)
                    rho, pval = spearmanr(sig, ret)
                    if math.isnan(rho):
                        print(f"  {name:12s}  IC = nan  (n={len(pairs)})")
                        continue
                    marker = " [OK clears 0.02]" if abs(rho) >= 0.02 else ""
                    print(
                        f"  {name:12s}  IC = {rho:+.4f}  "
                        f"(n={len(pairs)}, p={pval:.3f}){marker}"
                    )
                else:
                    print(f"  {name:12s}  insufficient data (n={len(pairs)})")
        except ImportError:
            print("  scipy not available — skipping")

    # ---- Phase milestone gauge ----
    _hr("Phase milestones (per arconian-v3.md §11 / §8.1)")
    print(
        f"  Closed paper trades:  {closed} / 40    "
        f"({closed / 40 * 100:.0f}% to Phase 1 evaluation gate)"
    )
    print(
        f"  Distinct scan days:   {distinct_days} / 60   "
        f"({distinct_days / 60 * 100:.0f}% to first IC recalibration window)"
    )
    print(
        f"  Labeled events:       {labeled} / 700  "
        f"({labeled / 700 * 100:.1f}% to logistic regression prerequisite)"
    )
    print(
        f"  Total signal events:  {total} / 3000  "
        f"({total / 3000 * 100:.1f}% to ML training pool target)"
    )

    conn.close()
    print()
    print("=" * 70)
    print("END OF SURVEY")


if __name__ == "__main__":
    main()
