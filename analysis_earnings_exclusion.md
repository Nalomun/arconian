# Earnings Exclusion + Return-Cap Sensitivity Analysis

**Thesis subset:** top composite decile × delay > 0.3
**Earnings proximity threshold:** ±3 calendar days (≈ ±1 trading day, §3.5.1)
**Cost threshold:** 120 bps (§5 long-side round-trip)

yfinance coverage: 1371 / 1799 thesis tickers (76%)

---

## Part 1: Return-Cap Sensitivity

Effect of capping extreme returns on mean forward return.

| Cap | Subset | n | Mean (bps) | Median (bps) |
|-----|--------|---|-----------|-------------|
| |ret| <= 20% | thesis (top×delay>0.3) | 32,819 | +23.0 | +10.1 |
| |ret| <= 20% | top decile (all delay) | 126,664 | +14.6 | +14.9 |
| |ret| <= 20% | bottom decile | 128,115 | +11.6 | +8.1 |
| |ret| <= 50% | thesis (top×delay>0.3) | 33,493 | +35.1 | +10.8 |
| |ret| <= 50% | top decile (all delay) | 128,802 | +21.6 | +15.6 |
| |ret| <= 50% | bottom decile | 128,944 | +15.4 | +8.4 |
| |ret| <= 100% | thesis (top×delay>0.3) | 33,577 | +42.2 | +10.9 |
| |ret| <= 100% | top decile (all delay) | 128,958 | +26.1 | +15.8 |
| |ret| <= 100% | bottom decile | 128,983 | +16.8 | +8.4 |
| |ret| <= 200% | thesis (top×delay>0.3) | 33,587 | +45.8 | +11.0 |
| |ret| <= 200% | top decile (all delay) | 128,978 | +28.1 | +15.8 |
| |ret| <= 200% | bottom decile | 128,987 | +17.1 | +8.5 |
| uncapped | thesis (top×delay>0.3) | 33,597 | +55.2 | +11.0 |
| uncapped | top decile (all delay) | 128,988 | +30.5 | +15.8 |
| uncapped | bottom decile | 128,988 | +17.3 | +8.5 |

---

## Part 2: Earnings Coverage

| Metric | Value |
|--------|-------|
| Unique thesis tickers | 1,799 |
| Tickers with yfinance earnings data | 1,371 (76%) |
| Thesis rows within ±3 cal days of earnings | 1,017 (3.0%) |
| Thesis rows with unknown earnings date | 9,471 (28.2%) |

---

## Part 3: Combined Filter Analysis (thesis subset only)

| Filter | n | Mean (bps) | Median (bps) | After cost (120 bps) |
|--------|---|-----------|-------------|--------------------------|
| Original (no filters) | 33,597 | +55.2 | +11.0 | -64.8 |
| Earnings excluded (±3d cal; unknowns kept) | 32,580 | +52.9 | +10.4 | -67.1 |
| Earnings excluded (confirmed coverage only) | 23,109 | +59.3 | +26.5 | -60.7 |
| Earnings excl + |ret| <= 100% | 32,560 | +39.5 | +10.3 | -80.5 |
| Earnings excl + |ret| <= 50% | 32,477 | +32.4 | +10.2 | -87.6 |
| Earnings excl + |ret| <= 20% | 31,852 | +21.9 | +9.9 | -98.1 |

---

## Interpretation

After earnings exclusion and ±100% return cap, the thesis-subset mean (+39.5 bps) is positive but below the 120 bps cost threshold (after-cost: -80.5 bps). Median is +10.3 bps. The signal has information content but the edge does not cover friction in the current backtest universe. The live system adds options signal, retail penalty, and tighter spread/liquidity filters — these may improve the ratio, but treat as unconfirmed.