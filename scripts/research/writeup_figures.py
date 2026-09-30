#!/usr/bin/env python3
"""
Figures for docs/ARCONIAN_WRITEUP.md.

Recomputes the two small tables the figures need from the recovered backtest CSV
(same definitions as scripts/analyze_backtest.py and scripts/research/daily_ic_check.py),
writes them to analysis/ as CSVs so every plotted number is on disk, checks them
against the values already recorded in PROJECT_HISTORY_ARCONIAN.md and
analysis/daily_ic_check.md, then renders:

  docs/img/decile_lift_pooled_vs_within_day.png
  docs/img/pooled_ic_decomposition.png

Read-only with respect to production code, config and the database.
"""
from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSV = os.path.join(ROOT, "backtest_2024-01-01_2026-04-21.csv")
AN = os.path.join(ROOT, "analysis")
IMG = os.path.join(ROOT, "docs", "img")
SCORES = ["composite", "volume", "return_mag", "sector_rs", "delay"]

# Reference palette (dataviz skill, light mode)
SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
S1, S2 = "#2a78d6", "#eb6834"   # categorical slots 1 and 2, validated adjacent pair

# Values already on record, used as a cross-check
POOLED_DECILE_REF = [17.3, 15.8, 19.3, 22.0, 23.9, 27.4, 26.5, 30.3, 32.7, 30.5]        # PROJECT_HISTORY §3.2
WITHIN_DECILE_REF = [23.7, 20.8, 24.2, 23.4, 26.0, 23.1, 25.3, 22.7, 24.3, 29.2]        # daily_ic_check.md §4
DECOMP_REF = {  # daily_ic_check.md §5: pooled, between, within
    "composite": (0.0131, 0.0127, 0.0005), "volume": (0.0163, 0.0104, 0.0059),
    "return_mag": (0.0134, 0.0138, -0.0004), "sector_rs": (0.0028, 0.0026, 0.0002),
    "delay": (-0.0243, -0.0001, -0.0242),
}


def style_axes(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.spines["bottom"].set_linewidth(1)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=1)
    ax.set_axisbelow(True)
    for lab in ax.get_xticklabels() + ax.get_yticklabels():
        lab.set_color(INK2)


def main():
    os.makedirs(IMG, exist_ok=True)
    df = pd.read_csv(CSV, parse_dates=["date"])

    # ---- Table 1: pooled decile means vs within-day decile means (bps) -----
    pooled_dec = pd.qcut(df["composite"], 10, labels=False) + 1
    pooled = df.groupby(pooled_dec)["fwd_return_3d"].mean() * 1e4
    pct = df.groupby("date")["composite"].rank(method="first", pct=True)
    day_dec = np.minimum((pct * 10).astype(int) + 1, 10)
    within = df.groupby([df["date"], day_dec])["fwd_return_3d"].mean().unstack().mean() * 1e4
    t1 = pd.DataFrame({"decile": range(1, 11),
                       "pooled_mean_bps": pooled.values,
                       "within_day_mean_bps": within.values})
    t1.to_csv(os.path.join(AN, "decile_means_pooled_vs_within_day.csv"), index=False, float_format="%.2f")
    assert np.allclose(t1["pooled_mean_bps"].round(1), POOLED_DECILE_REF, atol=0.11), t1
    assert np.allclose(t1["within_day_mean_bps"].round(1), WITHIN_DECILE_REF, atol=0.11), t1

    # ---- Table 2: pooled Spearman split into between-day and within-day ----
    rows = []
    for sc in SCORES:
        v = df.dropna(subset=[sc])
        rx = v[sc].rank(method="average")
        ry = v["fwd_return_3d"].rank(method="average")
        gx = rx.groupby(v["date"]).transform("mean")
        gy = ry.groupby(v["date"]).transform("mean")
        denom = rx.std(ddof=0) * ry.std(ddof=0)
        cov_b = ((gx - rx.mean()) * (gy - ry.mean())).mean()
        cov_w = ((rx - gx) * (ry - gy)).mean()
        rows.append({"score": sc, "pooled_ic": (cov_b + cov_w) / denom,
                     "between_day": cov_b / denom, "within_day": cov_w / denom})
    t2 = pd.DataFrame(rows)
    t2.to_csv(os.path.join(AN, "pooled_ic_decomposition.csv"), index=False, float_format="%.5f")
    for _, r in t2.iterrows():
        ref = DECOMP_REF[r["score"]]
        assert np.allclose([r["pooled_ic"], r["between_day"], r["within_day"]], ref, atol=0.00011), (r, ref)

    plt.rcParams.update({"font.family": "sans-serif", "font.size": 10, "text.color": INK,
                         "axes.labelcolor": INK2, "figure.facecolor": SURFACE})
    from matplotlib.patches import Patch

    def titles(ax, title, subtitle):
        ax.text(0, 1.10, title, transform=ax.transAxes, fontsize=12, color=INK, va="bottom")
        ax.text(0, 1.035, subtitle, transform=ax.transAxes, fontsize=8.5, color=MUTED, va="bottom")

    # ---- Figure A -----------------------------------------------------------
    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=160)
    style_axes(ax)
    x = np.arange(1, 11)
    w, gap = 0.36, 0.03
    ax.bar(x - w / 2 - gap / 2, t1["pooled_mean_bps"], w, color=S1, linewidth=0)
    ax.bar(x + w / 2 + gap / 2, t1["within_day_mean_bps"], w, color=S2, linewidth=0)
    ax.set_xlim(0.4, 10.6)
    ax.set_ylim(0, 37)
    ax.set_xticks(x)
    ax.set_xticklabels([f"D{i}" for i in x])
    ax.set_yticks([0, 10, 20, 30])
    ax.set_xlabel("Composite-score decile (D1 = lowest score, D10 = highest)", color=INK2)
    ax.set_ylabel("Mean 3-day forward return (bps)", color=INK2)
    for xi in (1, 10):   # selective direct labels: the two deciles the spread is built from
        for series, off in (("pooled_mean_bps", -w / 2 - gap / 2), ("within_day_mean_bps", w / 2 + gap / 2)):
            val = t1.loc[xi - 1, series]
            ax.text(xi + off, val + 0.6, f"{val:.0f}", ha="center", va="bottom", fontsize=8.5, color=INK2)
    ax.text(0.5, 36.4, "Pooled:  D10 − D1 = +13.2 bps", fontsize=9, color=INK2, va="top")
    ax.text(0.5, 34.0, "Within-day:  D10 − D1 = +5.5 bps, Newey-West t = 0.67", fontsize=9, color=INK2, va="top")
    ax.legend(handles=[Patch(color=S1, label="Pooled: deciles formed over all 1.29M rows at once"),
                       Patch(color=S2, label="Within-day: deciles formed each day, means averaged over 362 days")],
              loc="upper left", bbox_to_anchor=(0, -0.13), frameon=False, fontsize=8.5, labelcolor=INK2, ncol=1)
    titles(ax, "The decile lift is an artifact of pooling across days",
           "Backtest 2024-11-01 → 2026-04-15, 4,251 tickers.  Source: analysis/decile_means_pooled_vs_within_day.csv")
    fig.tight_layout(rect=(0, 0, 1, 0.99))
    fig.savefig(os.path.join(IMG, "decile_lift_pooled_vs_within_day.png"), facecolor=SURFACE)
    plt.close(fig)

    # ---- Figure B -----------------------------------------------------------
    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=160)
    style_axes(ax)
    ax.spines["bottom"].set_visible(False)
    ax.axhline(0, color=AXIS, linewidth=1, zorder=1)
    x = np.arange(len(SCORES))
    w, gap = 0.34, 0.04
    ax.bar(x - w / 2 - gap / 2, t2["between_day"], w, color=S1, linewidth=0, zorder=2)
    ax.bar(x + w / 2 + gap / 2, t2["within_day"], w, color=S2, linewidth=0, zorder=2)
    for xi, (_, r) in zip(x, t2.iterrows()):
        for off, val in ((-w / 2 - gap / 2, r["between_day"]), (w / 2 + gap / 2, r["within_day"])):
            va, dy = ("bottom", 0.0006) if val >= 0 else ("top", -0.0006)
            ax.text(xi + off, val + dy, f"{val:+.4f}", ha="center", va=va, fontsize=8, color=INK2)
    ax.set_xlim(-0.6, len(SCORES) - 0.4)
    ax.set_ylim(-0.03, 0.02)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{s}\npooled IC {t2.loc[i, 'pooled_ic']:+.4f}" for i, s in enumerate(SCORES)])
    ax.set_yticks([-0.03, -0.02, -0.01, 0, 0.01, 0.02])
    ax.set_ylabel("Contribution to pooled Spearman IC", color=INK2)
    ax.legend(handles=[Patch(color=S1, label="Between-day part: whole cross-section co-moving from day to day"),
                       Patch(color=S2, label="Within-day part: ranking stocks against each other on a given day")],
              loc="upper left", bbox_to_anchor=(0, -0.16), frameon=False, fontsize=8.5, labelcolor=INK2, ncol=1)
    titles(ax, "Where each score's pooled IC comes from",
           "Exact split of the pooled rank correlation; the two parts sum to the pooled IC.  Source: analysis/pooled_ic_decomposition.csv")
    fig.tight_layout(rect=(0, 0, 1, 0.99))
    fig.savefig(os.path.join(IMG, "pooled_ic_decomposition.png"), facecolor=SURFACE)
    plt.close(fig)

    print(t1.round(1).to_string(index=False))
    print(t2.round(4).to_string(index=False))
    print("figures written to", IMG)


if __name__ == "__main__":
    main()
