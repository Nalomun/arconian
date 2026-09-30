# Task: Data Quality Audit + Backtest Window Fix

## Context

The first historical backtest (`backtest_2024-01-01_2026-04-21.csv`) and the follow-up stratified IC analysis (`analysis_2026-04-21.md`) surfaced two issues that need to be resolved before the analysis can be trusted:

1. **Missing 10 months of data.** The backtest was called with `--start 2024-01-01` but the CSV only contains data from 2024-11-01 onward. The original backtest log shows `universe: 0 tickers` for every month from January through October 2024, then suddenly 3,094 tickers in November. This is almost certainly the `MIN_HISTORY = 120` filter interacting with however much history was loaded into `symbol_data` — Norgate is being asked for ~2 years pre-start (per the `extended_start` calculation), but it looks like the prefilter and/or full-history load is not actually reaching back far enough for symbols to have 120 trading days available before November 2024.

2. **124 extreme forward returns.** The CSV contains 123 observations with 3-day forward returns > +100% and 1 observation < −90%. At 0.01% of the dataset this looks small, but they are concentrated in the tails of the high-composite and high-delay subsets and are heavily distorting the means we're using to evaluate the thesis. The thesis-subset (top decile × delay > 0.3) has mean +55.2 bps but median +11.0 bps — the gap is being driven by these tail observations.

These two tasks can be done in parallel; treat them as one work session that produces two outputs (an audit report and a fixed/rerun backtest) but they don't depend on each other.

This is again a **scoped task**. The audit is read-only. The backtest fix is a small, surgical change to one script. Do not refactor anything else.

---

## Task 1: Audit the extreme forward returns

Create `scripts/audit_extreme_returns.py` that:

1. Loads `backtest_2024-01-01_2026-04-21.csv` and filters to rows where `abs(fwd_return_3d) > 0.20`. Report the total count.

2. For each of these rows, looks up the actual price history from Norgate using the existing `data/norgate_adapter.py` (instantiate `NorgateAdapter` the same way the backtest does). Pull the daily Close for `ticker` covering the window from 2 trading days before `date` through 5 trading days after.

3. Classifies each extreme observation into one of the following buckets and writes a `audit_extreme_returns.csv` with columns: `ticker, date, composite, delay, fwd_return_3d_csv, fwd_return_3d_recomputed, classification, notes`.

   Classification rules:
   - **`real_move`**: recomputed return matches CSV return within ±5%, AND the price series shows smooth daily transitions (no single-day gap > 50% that doesn't appear in the CSV's adjusted prices). These are real biotech/M&A/earnings-shock events.
   - **`split_artifact`**: recomputed return differs from CSV by > 5% AND there is a single-day price ratio in the lookup that suggests a split (price jumps by a clean ratio like 2x, 3x, 0.5x, 0.1x). Note the suspected split ratio in `notes`.
   - **`computation_mismatch`**: recomputed return differs from CSV by > 5% but no obvious split pattern. Flag for manual review.
   - **`norgate_lookup_failed`**: ticker not found, or insufficient history returned. Note in `notes`.

4. Reports a summary to stdout:
   - Total extreme observations
   - Count by classification
   - For `real_move`: top 10 by absolute return, with ticker, date, return, and a one-line note like "biotech catalyst suspected" if helpful (but don't over-claim — just note the magnitude).
   - For `split_artifact`: count, and the most common suspected split ratios.
   - **Most important number:** what fraction of the extreme observations are `real_move` vs. artifacts? This determines whether the means in the analysis are valid fat-tailed distributions or polluted by data errors.

5. Also computes and reports: if you exclude all non-`real_move` observations, what would the means in Analysis 3 of `analysis_2026-04-21.md` become? Specifically:
   - Top decile × delay > 0.3 mean (currently +55.2 bps with all extremes; recompute with artifacts excluded)
   - Top decile mean across all delay (currently +30.5 bps)
   - Bottom decile mean (currently +17.3 bps)
   
   Note: this requires re-loading the full CSV and recomputing decile means after dropping the flagged rows. Acceptable to do this in the same script.

This should take a few minutes of runtime — Norgate calls are fast, and you're only checking ~125 rows.

---

## Task 2: Fix the universe history window

The root cause of the 10-month data gap is in `scripts/backtest.py`. Look at the relevant section (around lines 380–400 in the run_backtest function):

```python
# Extend 2 years back for delay score weekly history
year_offset = int(start[:4]) - 2
extended_start = f"{year_offset}{start[4:]}"
```

This pushes `extended_start` to 2022-01-01 for a 2024-01-01 backtest start. That should be enough. But look at the prefilter:

```python
def _quick_prefilter(
    norgate: NorgateAdapter,
    symbols: list[str],
    end_date: date,
    lookback_days: int = 25,
) -> list[str]:
    ...
    approx_start = date(start.year, start.month, max(1, start.day - 40))
```

The prefilter is using `end_date - 40 days` as the start of its lookup, regardless of `lookback_days`. So it's only checking the most recent ~40 calendar days for price/ADTV. That's fine for the *prefilter* (it's just a fast first cut), but then the full-history load uses `norgate_start = extended_start`, which should give 2+ years of data per symbol.

So the prefilter is probably not the issue. The actual issue is more likely:

**Diagnose, don't guess.** Add temporary diagnostic logging (or a small standalone diagnostic script `scripts/diagnose_history_gap.py`) that:

1. Picks 5 representative tickers that *did* appear in the November 2024 universe (e.g., from the existing CSV, find tickers with a `2024-11-01` row).
2. For each, calls `norgate.get_price_history(sym, date(2022, 1, 1), date(2024, 11, 1))` and reports: how many rows are returned, what the actual first date in the data is, and what the last date is.
3. Then for the same tickers, simulates the `_passes_filters` check at `as_of = pd.Timestamp('2024-06-01')` and reports whether each ticker passes — and if not, *why* (history length too short? price too low? ADTV too low?).
4. Repeats step 3 for `as_of = pd.Timestamp('2024-03-01')` and `pd.Timestamp('2024-01-15')`.

The expected finding is one of these:
- **A:** Norgate is only returning data from ~2024-08 onward for these tickers (Norgate trial data limitation, or fetch is being clipped). In which case the fix is at the data layer.
- **B:** Norgate returns full history but `_passes_filters` fails for non-history reasons (e.g., the tickers were below $5M ADTV in early 2024). In which case the fix may be that the universe was genuinely smaller in early 2024, and we need to check whether the filter is too strict for that period.
- **C:** The prefilter is dropping these tickers before they reach `symbol_data` because the recent 40-day window had price < $2 or ADTV < $5M for some reason. In which case the fix is to relax the prefilter.

**After diagnosing, apply the minimal fix.** Do not rewrite the backtest. The fix should be:
- If A: report this as a Norgate trial-data constraint that cannot be fixed without subscribing or extending the trial. Document in the audit output. The backtest data is what it is.
- If B: report the finding. The "fix" is just acknowledgment that early 2024 universe was sparse. Possibly relax `MIN_ADTV_M` for diagnostic purposes only and report what universe size that produces.
- If C: change the prefilter's `approx_start` calculation to look back to `extended_start` instead of `end_date - 40 days`. This is a one-line change.

**Then rerun the backtest** with `python scripts/backtest.py --start 2024-01-01 --end 2026-04-21 --output backtest_2024-01-01_2026-04-21_v2.csv` and report the new universe sizes per month. Do not overwrite the original CSV — write to a new file with `_v2` suffix so we can compare.

If the diagnosis is A or B, do not rerun the backtest — there's nothing to fix. Just report the finding.

---

## Output

Three artifacts at the project root:

1. `audit_extreme_returns.csv` — per-row classification of the 124 extreme observations
2. `audit_extreme_returns_summary.md` — stdout summary written to a file: classification counts, recomputed means with artifacts excluded, and a clear yes/no on whether the original analysis means are reliable
3. `diagnose_history_gap.md` — findings from the diagnostic script: which of A/B/C is the cause, what (if anything) was fixed, and the new monthly universe sizes if a rerun was triggered

Console output should be a clean summary covering both tasks — no need for the full per-row dumps in the terminal.

## Don't

- Don't modify `data/norgate_adapter.py`, the signal modules, the universe modules, the risk engine, or the main backtest CSV. The backtest script is the only thing that may be modified, and only with a one-line change if diagnosis points to C.
- Don't kick off long-running operations without confirming the diagnosis first. The rerun is conditional on finding cause C. If it's A or B, stop and report.
- Don't try to "improve" the audit by including non-extreme observations or expanding the analysis scope. This is a focused data-quality check, not another full analysis.
- Don't re-paraphrase the whitepaper. Just cite section numbers if needed (probably won't be needed for this task).
