# History Gap Diagnostic Report

## Background

Backtest `--start 2024-01-01` logged `universe: 0 tickers` from January through
October 2024, then 3,094 tickers in November 2024. This report identifies why.

## Sample Tickers

Selected from 2024-11-01 rows in the CSV: `A, AA, AAAU, AAGIY, AAL`

## Norgate History Coverage (requested: 2022-01-01 → 2024-11-01)

All five sample tickers returned identical coverage: 136 rows, 2024-04-22 → 2024-11-01.

_Per-ticker rows removed from the public snapshot: they are values from licensed Norgate data._

## Point-in-Time Filter Simulation

For all five tickers the history-length filter fails on every simulated date: 29 < 120 rows at 2024-06-01, 0 < 120 at 2024-03-01 and at 2024-01-15.

## Root Cause: A

Norgate returned data but it starts too late for the history-length filter to pass at 2024-01-15. Even with the full-history request the coverage is insufficient for the early backtest dates.

## Action

**No code change warranted.** The `_quick_prefilter` and `_passes_filters`
logic is correct. The empty universe in Jan–Oct 2024 is explained by Norgate
trial data coverage, not a bug.

The backtest data is valid for its actual window: **November 2024 → April 2026**
(approximately 17 months, ~365 trading days).

## Impact on Thesis Verifiability

- The 17-month window covers a single macro regime (post-election rally through
  early 2026 volatility). Edge persistence cannot be confirmed across multiple cycles.
- Phase 1 live trading provides the first genuinely independent out-of-sample test.
- A full Norgate Platinum subscription (~$500/year) would unlock the pre-2024 data
  and enable a materially longer backtest. This is the only fix available.