# Per-day cross-sectional IC check of the backtest CSV

Run: 2026-09-28 13:35  ·  script: scripts/research/daily_ic_check.py

## 1. Input

- File: `backtest_2024-01-01_2026-04-21.csv` (built from licensed Norgate data and is not distributed)
- Byte count: 137,121,407 (expected 137,121,407) — MATCH
- Rows: 1,289,872  ·  tickers: 4,251  ·  trading days: 362  ·  2024-11-01 → 2026-04-15
- fwd_return_3d non-null: 1,289,872  ·  delay non-null: 867,707
- Stocks per day: min 3,094, median 3,513, max 4,097

Newey-West t-stats use a Bartlett kernel; the primary lag is 3 (3-day forward windows overlap by 2 days), lag 5 is shown as a robustness check. The naive t-stat (iid days) is shown for comparison only.

## 2. Per-day Spearman IC, all stocks

Each day: Spearman rank correlation across stocks between the score and fwd_return_3d. Days with fewer than 20 scored stocks are skipped (relevant only for `delay`, which is NULL for the first months).

| series | days | mean IC | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|
| composite | 362 | -0.0028 | 0.0748 | 49% | -0.72 | -0.54 | -0.52 |
| volume | 362 | +0.0047 | 0.0680 | 53% | +1.30 | +0.91 | +0.87 |
| return_mag | 362 | -0.0033 | 0.0728 | 50% | -0.87 | -0.86 | -0.86 |
| sector_rs | 362 | -0.0006 | 0.0403 | 53% | -0.30 | -0.23 | -0.22 |
| delay | 240 | -0.0281 | 0.1105 | 41% | -3.94 | -2.63 | -2.51 |

Per-day series written to `analysis/daily_ic_by_score.csv`.

Autocorrelation of the daily composite IC series (lags 1–5): +0.341, +0.242, +0.079, -0.020, -0.006

Composite per-day IC by thirds of the date range (by row count of days):

| third | dates | days | mean IC | % days > 0 | t (NW 3) |
|---|---|---|---|---|---|
| early | 2024-11-01 → 2025-04-29 | 121 | -0.0086 | 45% | -1.07 |
| middle | 2025-04-30 → 2025-10-20 | 120 | -0.0142 | 44% | -1.56 |
| late | 2025-10-21 → 2026-04-15 | 121 | +0.0142 | 57% | +1.53 |

## 3. Per-day Spearman IC within delay buckets

Same statistic, computed within (day, bucket). A (day, bucket) cell with fewer than 20 stocks is skipped; `days` is the number of cells that survive. Bucket membership is per row, so a stock's bucket can change day to day.

Stocks per (day, bucket): median across days

| bucket | days with >= 20 stocks | median stocks/day | first day with >= 20 |
|---|---|---|---|
| A: delay > 0.5 | 240 | 744 | 2025-05-01 |
| B: 0.3 < delay <= 0.5 | 240 | 486 | 2025-05-01 |
| C: 0.1 < delay <= 0.3 | 240 | 1,178 | 2025-05-01 |
| D: delay <= 0.1 | 240 | 1,128 | 2025-05-01 |
| E: delay NULL | 362 | 84 | 2024-11-01 |

### Bucket A: delay > 0.5  (rows: 179,345)

| series | days | mean IC | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|
| composite | 240 | +0.0027 | 0.0912 | 52% | +0.45 | +0.35 | +0.33 |
| volume | 240 | +0.0062 | 0.0814 | 54% | +1.17 | +0.84 | +0.79 |
| return_mag | 240 | -0.0016 | 0.0847 | 50% | -0.29 | -0.29 | -0.29 |
| sector_rs | 240 | -0.0011 | 0.0545 | 52% | -0.31 | -0.24 | -0.24 |
| delay | 240 | -0.0059 | 0.1071 | 48% | -0.85 | -0.60 | -0.59 |

### Bucket B: 0.3 < delay <= 0.5  (rows: 117,233)

| series | days | mean IC | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|
| composite | 240 | +0.0030 | 0.0820 | 52% | +0.58 | +0.43 | +0.40 |
| volume | 240 | +0.0020 | 0.0901 | 51% | +0.34 | +0.23 | +0.22 |
| return_mag | 240 | +0.0028 | 0.0714 | 52% | +0.61 | +0.62 | +0.61 |
| sector_rs | 240 | -0.0010 | 0.0610 | 50% | -0.25 | -0.19 | -0.18 |
| delay | 240 | -0.0034 | 0.0539 | 47% | -0.99 | -0.68 | -0.65 |

### Bucket C: 0.1 < delay <= 0.3  (rows: 288,687)

| series | days | mean IC | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|
| composite | 240 | +0.0098 | 0.0741 | 55% | +2.05 | +1.63 | +1.52 |
| volume | 240 | +0.0103 | 0.0730 | 59% | +2.19 | +1.51 | +1.41 |
| return_mag | 240 | +0.0056 | 0.0743 | 50% | +1.18 | +1.21 | +1.20 |
| sector_rs | 240 | -0.0024 | 0.0477 | 50% | -0.78 | -0.60 | -0.57 |
| delay | 240 | -0.0067 | 0.0429 | 45% | -2.42 | -1.63 | -1.55 |

### Bucket D: delay <= 0.1  (rows: 282,442)

| series | days | mean IC | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|
| composite | 240 | +0.0069 | 0.0870 | 53% | +1.22 | +1.01 | +0.95 |
| volume | 240 | +0.0081 | 0.0709 | 57% | +1.78 | +1.25 | +1.16 |
| return_mag | 240 | +0.0021 | 0.1056 | 52% | +0.31 | +0.31 | +0.30 |
| sector_rs | 240 | -0.0028 | 0.0476 | 48% | -0.90 | -0.72 | -0.73 |
| delay | 240 | -0.0083 | 0.1105 | 46% | -1.16 | -0.75 | -0.71 |

### Bucket E: delay NULL  (rows: 422,165)

| series | days | mean IC | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|
| composite | 362 | +0.0041 | 0.1407 | 49% | +0.55 | +0.44 | +0.43 |
| volume | 362 | +0.0084 | 0.1417 | 51% | +1.13 | +0.86 | +0.84 |
| return_mag | 362 | -0.0018 | 0.1384 | 49% | -0.25 | -0.22 | -0.22 |
| sector_rs | 362 | +0.0036 | 0.1024 | 51% | +0.68 | +0.53 | +0.54 |

## 4. Per-day decile spread (top decile minus bottom decile, by composite)

Each day, stocks are split into ten equal-count bins by composite rank; the spread is the mean fwd_return_3d of the top bin minus the mean of the bottom bin. Values in bps. The same is shown for the other scores.

| series | days | mean spread (bps) | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|
| composite | 362 | +5.4927 | 116.3973 | 51% | +0.90 | +0.67 | +0.65 |
| volume | 362 | +6.3429 | 121.9342 | 57% | +0.99 | +0.72 | +0.72 |
| return_mag | 362 | -2.8113 | 95.0897 | 48% | -0.56 | -0.51 | -0.50 |
| sector_rs | 362 | -1.2573 | 76.1154 | 48% | -0.31 | -0.24 | -0.23 |
| delay | 240 | -8.1523 | 96.8481 | 47% | -1.30 | -0.86 | -0.81 |

Composite only, additional detail:

- Time-series mean of daily top-decile mean return: +29.2 bps (NW3 t = +2.02)
- Time-series mean of daily bottom-decile mean return: +23.7 bps (NW3 t = +1.39)
- Median daily spread: +2.1 bps; p10 / p90 of daily spread: -129.8 / +144.7 bps
- Mean return by within-day decile (time-series average of the daily decile means, bps): D1 +23.7, D2 +20.8, D3 +24.2, D4 +23.4, D5 +26.0, D6 +23.1, D7 +25.3, D8 +22.7, D9 +24.3, D10 +29.2

Thesis subset (delay > 0.3, buckets A+B). Deciles by composite formed each day *within* that subset:

| series | days | mean spread (bps) | sd | % days > 0 | t (naive) | t (NW 3) | t (NW 5) |
|---|---|---|---|---|---|---|---|
| D10 − D1, within delay > 0.3 | 240 | +15.4735 | 156.1852 | 55% | +1.53 | +1.09 | +1.03 |
| D10 − mean of D1..D9, within delay > 0.3 | 240 | +16.5214 | 117.2793 | 55% | +2.18 | +1.64 | +1.59 |

- Time-series mean of the daily top-decile mean return within delay > 0.3: +50.5 bps (NW3 t = +3.41); the pooled version of this cell is the +55.2 bps in analysis_2026-04-21.md (row 30 of PROJECT_HISTORY).

Per-day spreads written to `analysis/daily_decile_spread.csv`.

For reference, the pooled (all rows, not per-day) composite D10 − D1 spread is +13.2 bps, matching the +13.2 bps in analysis_2026-04-21.md.

## 5. Why is the pooled IC positive when the per-day IC is about zero?

Pooled Spearman over all rows, then the same after removing each day's cross-sectional mean return (so that only within-day variation in returns remains), then after also demeaning the score by day.

| score | n | pooled IC (raw) | pooled IC, return demeaned by day | pooled IC, both demeaned by day | mean per-day IC (from §2) |
|---|---|---|---|---|---|
| composite | 1,289,872 | +0.0131 | -0.0006 | -0.0019 | -0.0028 |
| volume | 1,289,872 | +0.0163 | +0.0046 | +0.0051 | +0.0047 |
| return_mag | 1,289,872 | +0.0134 | -0.0028 | -0.0024 | -0.0033 |
| sector_rs | 1,289,872 | +0.0028 | -0.0003 | +0.0043 | -0.0006 |
| delay | 867,707 | -0.0243 | -0.0252 | -0.0251 | -0.0281 |

Between-day component. Each day's cross-sectional mean score against that day's cross-sectional mean return (one point per trading day):

| score | days | Spearman(day-mean score, day-mean return) | Pearson | NW3 t of Pearson slope proxy |
|---|---|---|---|---|
| composite | 362 | +0.007 | +0.045 | +0.74 |
| volume | 362 | -0.003 | +0.043 | +0.70 |
| return_mag | 362 | +0.028 | +0.075 | +1.29 |
| sector_rs | 362 | +0.091 | +0.121 | +1.69 |
| delay | 240 | -0.009 | +0.009 | +0.09 |

Exact decomposition of the pooled Spearman. Spearman is the Pearson correlation of the pooled ranks. The covariance of the pooled ranks splits exactly into a between-day part (covariance of the day means of the ranks) and a within-day part (covariance of the rank deviations from their day means). Each part is divided by the same product of pooled rank standard deviations, so the two columns sum to the pooled IC.

| score | pooled IC | between-day part | within-day part | corr of day-mean ranks (row-weighted) | between-day share of rank variance: score / return |
|---|---|---|---|---|---|
| composite | +0.0131 | +0.0127 | +0.0005 | +0.072 | 16.0% / 19.1% |
| volume | +0.0163 | +0.0104 | +0.0059 | +0.065 | 13.6% / 19.1% |
| return_mag | +0.0134 | +0.0138 | -0.0004 | +0.092 | 11.9% / 19.1% |
| sector_rs | +0.0028 | +0.0026 | +0.0002 | +0.098 | 0.4% / 19.1% |
| delay | -0.0243 | -0.0001 | -0.0242 | -0.008 | 0.2% / 15.6% |

Note the between-day share of the *rank* variance of returns is much larger than the between-day share of the raw return variance below: ranking compresses the fat tails, so day-to-day shifts in the whole cross-section move the ranks a lot.

Share of fwd_return_3d variance that is between-day (variance of day means / total variance): 10.9%. Share of composite variance that is between-day: 17.1%.

Time-series of daily cross-sectional mean return and mean composite, by calendar quarter:

| quarter | days | rows | mean fwd 3d return (bps) | mean composite | mean volume score | mean return_mag score |
|---|---|---|---|---|---|---|
| 2024Q4 | 41 | 131,558 | +16.8 | 0.5205 | 0.5517 | 0.5040 |
| 2025Q1 | 60 | 201,233 | -29.6 | 0.5291 | 0.5544 | 0.5293 |
| 2025Q2 | 62 | 213,378 | +60.6 | 0.4832 | 0.5059 | 0.4801 |
| 2025Q3 | 64 | 225,525 | +42.0 | 0.4794 | 0.5211 | 0.4831 |
| 2025Q4 | 64 | 237,732 | +21.7 | 0.4792 | 0.5120 | 0.4964 |
| 2026Q1 | 61 | 239,476 | -2.7 | 0.5113 | 0.5691 | 0.5462 |
| 2026Q2 | 10 | 40,970 | +208.4 | 0.4457 | 0.4394 | 0.4679 |

_Runtime 12s._
