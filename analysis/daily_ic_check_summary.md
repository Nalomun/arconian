# Per-day IC check — findings (2026-09-28)

**The composite does not rank stocks within a day. Cross-sectionally the signal is dead.**

Source: `backtest_2024-01-01_2026-04-21.csv` (137,121,407 bytes, verified), built from licensed Norgate data and is not distributed.
Script: `scripts/research/daily_ic_check.py`. Full tables: `analysis/daily_ic_check.md`.
Per-day series: `analysis/daily_ic_by_score.csv`, `analysis/daily_ic_bucket_{A..E}.csv`, `analysis/daily_decile_spread.csv`.

Newey-West t-stats use a Bartlett kernel with 3 lags (5 lags shown as a check; conclusions do not change).
The daily composite-IC series has autocorrelation +0.34, +0.24, +0.08 at lags 1–3, as expected from the 2-day overlap of the 3-day forward windows, so the naive t-stats overstate significance by roughly a third.

## 1. Per-day Spearman IC, all stocks (362 days)

| score | mean IC | sd | % days > 0 | t (NW 3) |
|---|---|---|---|---|
| composite | −0.0028 | 0.075 | 49% | −0.54 |
| volume | +0.0047 | 0.068 | 53% | +0.91 |
| return_mag | −0.0033 | 0.073 | 50% | −0.86 |
| sector_rs | −0.0006 | 0.040 | 53% | −0.23 |
| delay (240 days) | −0.0281 | 0.111 | 41% | −2.63 |

Nothing is distinguishable from zero except `delay`, which is reliably **negative**: within a day, higher-delay stocks earn lower 3-day returns. That is the opposite of the thesis. The composite's per-day IC is negative in the early and middle thirds and positive in the late third; none of the three is significant (NW t between −1.6 and +1.5).

## 2. Within delay buckets (240 days for A–D, 362 for E)

Composite per-day IC by bucket: A +0.003 (t +0.35), B +0.003 (t +0.43), C +0.010 (t +1.63), D +0.007 (t +1.01), E +0.004 (t +0.44). Twenty-four score×bucket cells were tested; the largest t-stat is +1.63 (composite, bucket C). No cell clears conventional significance, and the two high-delay buckets the thesis is about are the weakest of the four with delay scores. `delay` itself is negative in every bucket.

## 3. Per-day decile spread (composite, D10 − D1)

| | mean (bps) | sd | % days > 0 | t (NW 3) |
|---|---|---|---|---|
| all stocks, 362 days | +5.5 | 116 | 51% | +0.67 |
| within delay > 0.3, 240 days | +15.5 | 156 | 55% | +1.09 |
| D10 minus D1–D9, within delay > 0.3 | +16.5 | 117 | 55% | +1.64 |

The median daily spread is +2 bps; the 10th/90th percentiles are −130 / +145 bps. The time-series average of within-day decile means is flat: D1 +23.7, D5 +26.0, D10 +29.2 bps. The pooled +13.2 bps spread in `analysis_2026-04-21.md` reproduces exactly but is not a per-day quantity.

The headline "+55.2 bps for top decile × delay > 0.3" reproduces as a time-series mean of +50.5 bps (NW t +3.4), but that is the cell's *raw* return, not its return relative to alternatives. The whole universe averaged +24.6 bps per 3-day window over this period, and the delay > 0.3 subset averaged +35 bps. The selection component (top decile vs the other nine deciles within the same subset) is +16.5 bps with t +1.64, before costs of 75–120 bps.

## 4. Why pooled IC is +0.013 while the average per-day IC is about zero

Spearman is the Pearson correlation of pooled ranks, and the covariance of pooled ranks splits exactly into a between-day part and a within-day part:

| score | pooled IC | between-day part | within-day part |
|---|---|---|---|
| composite | +0.0131 | +0.0127 | +0.0005 |
| volume | +0.0163 | +0.0104 | +0.0059 |
| return_mag | +0.0134 | +0.0138 | −0.0004 |
| sector_rs | +0.0028 | +0.0026 | +0.0002 |
| delay | −0.0243 | −0.0001 | −0.0242 |

97% of the composite's pooled IC is between-day: on days when the whole cross-section had high volume and return-magnitude percentiles, the whole cross-section also tended to have higher forward returns over the next three days. Subtracting each day's cross-sectional mean return from every row and recomputing the pooled Spearman gives −0.0006 (composite), +0.0046 (volume), −0.0028 (return_mag), −0.0003 (sector_rs). The direct test confirms the time-effect explanation.

The between-day correlation itself is weak (rank-space correlation of day means +0.07, 362 points) and would not be tradeable as a timing signal from these numbers. It shows up as a large pooled IC because the "n" of 1.29 million treats every stock on the same day as an independent observation of one common shock.

`delay` is the exception: its pooled IC is almost entirely within-day, so it is the one genuinely cross-sectional effect in the dataset, and its sign is wrong for the thesis.

## Caveats carried over from PROJECT_HISTORY §4.2

This check inherits the survivorship prefilter and the missing market-cap filter. Both would tend to flatter, not hurt, the numbers above. The delay score is NULL before 2025-05-01, so bucket E and the early third of the sample are the same rows.
