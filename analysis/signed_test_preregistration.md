# Signed-return test — pre-registration

Written 2026-09-29 and committed alone, before any statistic relating a score to a signed or raw forward return was computed on the matched sample defined below. Before this commit I had seen only:
- the CRSP pull diagnostics (`crsp_pull/pull_report.txt`, summarized in `analysis/signed_test_data_request.md`)
- the sample-definition counts in §2
- the earlier raw-return results on the full CSV (`analysis/daily_ic_check.md`, 2026-09-28)

## 1. Question

Does the composite predict continuation **in the direction of the day-t move**? The live system traded in the direction of the day's return: in practice its directional check reduced to the sign of the 1-day return (`signals/signal_engine.py:565`, `signals/directional_confirmation.py`). Volume surprise, return magnitude and delay are unsigned, so the earlier test against raw forward returns could not detect directional drift. A drift thesis predicts that high scorers continue in the direction of their move.

## 2. Data and sample (fixed)

- **Scores and outcome.** `backtest_2024-01-01_2026-04-21.csv` (137,121,407 bytes, from `1acd1a1`). The outcome is the CSV's own `fwd_return_3d` (close(t) → close(t+3), Norgate total-return adjusted), unchanged from the raw test.
- **Day-t direction.** From CRSP CIZ `crsp.stkdlysecuritydata`, pulled by `scripts/research/crsp_pull_day_t_returns.py` into `crsp_pull/day_t_returns.parquet` (CRSP-licensed, gitignored).
  - **Primary sign: sign(DLYRETX)**, the price-only return, which is what the live system saw.
  - Robustness sign: sign(DLYRET), with distributions. The two disagree on 3,571 matched rows.
- **Row inclusion.**
  - `map_status == "ok"`: PERMNO found, and the CRSP-compounded 3-day forward total return agrees with the CSV's `fwd_return_3d` within 5 bps. The tolerance was fixed before the pull. Median disagreement is 3.7e-7, and 97.2% of rows agree within 1e-5.
  - The day-t return used for the sign is not exactly zero.
- **Primary sample: 952,002 rows over 288 trading days, 2024-11-01 → 2025-12-26.**
  - Drops 12,502 matched rows with DLYRETX = 0.
  - Stocks per day: at least 2,947.
- **Robustness sample (sign(DLYRET)):** 952,045 rows (drops 12,459 matched rows with DLYRET = 0).

**Coverage limit.** The CRSP stock data on this WRDS subscription ends 2025-12-31, and no newer stock library is available. The test therefore covers **288 of the original 362 days** and **excludes Jan–Apr 2026 entirely**:
- **Jan–Apr 2026 (71 days):** no CRSP data.
- **2025-12-29 to 12-31 (3 days):** the mapping check needs forward prices in January 2026, so no row on these days can be validated.

The full window can be rerun unchanged when the next annual CRSP update is released. Note that the 2026-09-28 raw test found its only positive (non-significant) composite IC in the late third, 2025-10-21 → 2026-04-15, which this sample mostly excludes. The raw-return control in §5 runs on exactly the same rows, so the raw-vs-signed comparison is not affected by this.

Other rows lost in the 288-day window (1,009,426 CSV rows):
- 31,365 with no PERMNO, mostly 128 five-character Y/F tickers that look like OTC ADRs, plus 9 preferred-style tickers
- 1,540 failing the 5 bps identity check
- 1,148 with a PERMNO but no CRSP row on date t

## 3. Definitions

- `dir_t = sign(DLYRETX_t)` ∈ {+1, −1}. +1 is a long trade, −1 a short trade.
- **Signed forward return:** `sfwd = dir_t × fwd_return_3d`.
- **Per-day IC:** Spearman rank correlation across stocks within one date, with average ranks for ties. This is the exact method of `scripts/research/daily_ic_check.py` (`per_group_spearman`). A (day, cell) with fewer than 20 stocks is skipped.
- **Newey-West t:** time-series mean of the daily series divided by its NW standard error (Bartlett kernel). **3 lags is the decision lag.** 5 lags is reported for reference only.
- **Within-day deciles:** each day, `rank(method="first", pct=True)` of the score, then `dec = min(floor(10·pct) + 1, 10)`. Only days with at least 200 stocks in the cell are kept. This is identical to `daily_ic_check.py`.
- **Costs:** 120 bps round trip for a long (`dir_t = +1`), 150 bps for a short (`dir_t = −1`), applied row by row: `net = sfwd − cost(dir_t)`.

## 4. Primary test and decision rule (confirmatory)

**Primary statistic:** the mean over the 288 days of the per-day Spearman IC of `composite` against `sfwd` on the primary sample, with its NW t (3 lags).

**Cost statistic:** each day, take the within-day top composite decile of the primary sample and compute the equal-weighted mean of `net` across it. Then take the time-series mean of that daily series over the 288 days. It is reported with its NW t (3 lags), but only the point estimate enters the rule.

**Verdict (exactly one applies):**

| verdict | condition |
|---|---|
| **alive** | primary NW3 t ≥ +2.0 **and** mean daily top-decile net signed return > 0 bps |
| **reversal** | primary NW3 t ≤ −2.0 |
| **dead** | anything else, including primary t ≥ +2.0 with the cost statistic ≤ 0 (reported as "dead: not tradeable") |

"Reversal" describes the sign of the primary statistic. Under this rule it does not make the signal tradeable, and any use of it would need its own pre-registered test on new data.

Nothing else below can change the verdict. The sign(DLYRET) robustness check, the 5-lag t, the raw control, and every exploratory result are reported whichever way they come out. If the robustness check disagrees with the primary result on the verdict category, that is reported plainly next to the verdict, and the verdict still stands as defined.

## 5. Control (reported side by side with the primary)

The per-day Spearman IC of `composite` against **raw** `fwd_return_3d` on the **same 952,002 rows**, with the same NW t. This separates the effect of signing from the effect of the reduced sample.

## 6. Secondary and exploratory (labeled as such in the report; no decision weight)

1. **Individual scores.** Per-day signed IC for `volume`, `return_mag`, `delay`, and `sector_rs`. `sector_rs` is already signed, so it is reported on both raw and signed returns. Each is shown next to its raw-return IC on the same rows.
2. **Decile spreads.** Within-day signed decile spreads by composite: D10 − D1 and D10 − mean(D1..D9). Each is reported with mean, median, % of days positive, and NW t (3 and 5 lags).
3. **Delay buckets.** The same statistics within delay buckets A (> 0.5), B (0.3–0.5], C (0.1–0.3], D (≤ 0.1), and E (NULL), using the same boundaries as before. A–D have 166 days each, because delay is NULL before 2025-05-01. Bucket E has 288 days, and a median of 78 stocks per day, so its decile statistics are mostly skipped by the 200-stock rule.
4. **Thesis subset (delay > 0.3, 166 days).** Signed per-day IC, within-subset decile spreads, and the top-decile net signed return after direction-specific costs.
5. **Cost breakdown.** Top-decile net signed return split by long vs short trades. The long/short mix of the top decile is reported too.
6. **Decomposition.** Between-day / within-day decomposition of the pooled signed Spearman IC, using the exact rank-covariance split from `daily_ic_check.py` §5, plus the pooled IC after demeaning `sfwd` by day.
7. **Reversal check.** A significantly negative signed IC (NW3 t ≤ −2.0) in any secondary cell is reported as exploratory.
8. **Robustness.** Everything in §4 repeated with sign(DLYRET) on its own sample.
9. **Matched vs unmatched rows.** A descriptive comparison within the 288-day window: composite (mean, median, p10/p90), share of rows in the within-day top composite decile (deciles formed on the full CSV row set for that day), share with non-NULL delay, and delay mean/median. The excluded Jan–Apr 2026 rows are summarized the same way. No returns are used.

Roughly 5 scores × 6 cells × 2 spread types, plus the robustness and control runs, add up to several dozen secondary statistics. At the 5% level a few are expected to cross |t| = 2 by chance. None of them is interpreted as evidence.

## 7. Known biases carried into this test

- **Survivorship prefilter.** The symbol set was chosen by price and liquidity in April 2026, so names that delisted or collapsed before then are absent.
- **No market-cap filter.** The universe is roughly all liquid U.S. equities, not $500M–$2B small caps.

For the earlier raw-return test these biases plausibly flattered returns. **For a signed test the direction of their effect is not known in advance.** Survivorship removes names whose moves ended in collapse, and it is not known whether those moves, signed by the day-t direction, would have added to or subtracted from the result.

The matched sample also drops most OTC ADR-like and preferred tickers, and it ends 2025-12-26.

## 8. Implementation

`scripts/research/signed_test.py` (new; read-only) writes `analysis/signed_test.md` (full tables), the per-day series CSVs under `analysis/signed_*`, and `analysis/signed_test_summary.md`. The summary's first line is the verdict under §4.
