# Extreme Forward Return Audit Summary

**Backtest file:** backtest_2024-01-01_2026-04-21.csv
**Threshold:** |fwd_return_3d| > 20%
**Total extreme observations:** 12950

## Classification Counts

- **real_move:** 12948 (100.0%)
- **computation_mismatch:** 2 (0.0%)

**Confirmed real moves:** 12948 (100.0%)
**Artifacts / unconfirmed:** 2 (0.0%)

## Top 10 Real Moves by Absolute Return

The ten largest 3-day moves ranged from +305.2% to +613.6%, across six tickers; all ten were classified `real_move`.

_Per-ticker rows removed from the public snapshot: they are values from licensed Norgate data._

## Recomputed Analysis 3 Means (artifacts excluded)

| Subset | Original | Clean | Delta | Reliable? |
|--------|----------|-------|-------|-----------|
| Top decile × delay > 0.3 | +55.2 bps | +55.2 bps | +0.0 | YES |
| Top decile (all delay)   | +30.5 bps | +30.5 bps | +0.0 | YES |
| Bottom decile            | +17.3 bps | +17.4 bps | +0.1 | YES |

## Verdict: Are the original analysis means reliable?

**YES** — 100.0% of extreme observations are confirmed real moves (Norgate total-return data agrees with the CSV return within ±5 pp). The mean/median gap in Analysis 3 reflects genuine fat-tailed distributions (biotech/M&A/earnings shocks), not data errors. The means are valid but skewed by rare extreme events.