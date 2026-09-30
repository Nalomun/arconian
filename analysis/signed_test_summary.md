**Verdict under the pre-registered rule: DEAD.** The composite does not predict continuation in the direction of the day-t move (per-day signed IC −0.004, NW t −1.18), and its top decile loses 136 bps per trade after direction-appropriate costs.

# Signed-return test: summary (2026-09-29)

- Pre-registration: `analysis/signed_test_preregistration.md`, committed alone as `daf7354` in the private development repository before any test was run (published file: git blob `a1812569fe85b9b76bc0209eac3478d6581ac004`).
- Script: `scripts/research/signed_test.py`
- Full tables: `analysis/signed_test.md`
- Per-day series: `analysis/signed_daily_ic.csv`, `analysis/signed_decile_spread.csv`, `analysis/signed_daily_topdecile.csv`

**Sample:**
- 952,002 stock-days over **288 of the original 362 trading days, 2024-11-01 → 2025-12-26**. The subscription's CRSP stock data ends 2025-12-31, so Jan–Apr 2026 is excluded entirely. The full window can be rerun unchanged when the next annual CRSP update is released.
- Direction = sign(DLYRETX_t), the price-only day-t return the live system saw.
- 12,502 matched rows with DLYRETX = 0 were dropped.
- Signed forward return = direction × `fwd_return_3d`.

## Primary result (confirmatory)

| | days | mean | % days > 0 | NW t (3) | NW t (5) |
|---|---|---|---|---|---|
| **Composite per-day IC vs signed fwd return** | 288 | **−0.0041** | 48% | **−1.18** | −1.21 |
| Control: composite per-day IC vs raw fwd return, same rows | 288 | −0.0101 | 43% | −1.67 | −1.64 |
| Top composite decile, gross signed return | 288 | −3.1 bps | 54% | −0.39 | −0.39 |
| **Top composite decile, net of 120 / 150 bps costs** | 288 | **−135.7 bps** | 12% | −16.88 | −16.74 |

Neither condition for "alive" is met: the IC t is not ≥ 2.0, and the net top-decile return is not > 0. The IC t is also not ≤ −2.0, so the verdict is not "reversal". Signing the return did not uncover a directional effect that the raw test had missed. The signed IC is as indistinguishable from zero as the raw one, and the top decile's gross signed return is −3 bps before costs.

**Robustness (no decision weight).** Using sign(DLYRET) instead gives the same result: signed IC −0.0040, NW t −1.16, top-decile net −135.6 bps. Verdict under the same rule: dead.

The raw control on this sample (−0.0101, t −1.67) is more negative than on the full 362-day sample (−0.0028). The excluded Jan–Apr 2026 stretch was where the earlier test found its only positive composite IC. Neither number is significant.

## Secondary results — EXPLORATORY, no decision weight

About 120 statistics are reported in `signed_test.md`. At |t| ≥ 2, several are expected by chance, and none of the items below is evidence of anything.

- **Individual scores, whole sample (signed per-day IC, NW3 t).**
  - volume −0.0031 (−0.92); return_mag −0.0051 (−0.98); delay −0.0014 (−0.18, 166 days).
  - sector_rs +0.0008 (+0.26) signed, −0.0006 (−0.17) raw.
  - None is distinguishable from zero.
  - The earlier negative delay effect is present on raw returns in this sample (−0.032, t −2.49) and absent on signed returns. So it is a level effect (high-delay names earned less), not a directional one.
- **Decile spreads, composite, whole sample.**
  - D10 − D1 signed: −2.6 bps (median +3.4, 52% of days positive, t −0.39).
  - D10 − rest: +1.4 bps (median +6.6, 56%, t +0.26).
  - Mean signed return is between −8 and 0 bps in every decile, with no ordering.
- **Direction split of the top decile.**
  - 58% longs: gross +14.7 bps (t +0.82), net −105.3.
  - 42% shorts: gross −28.1 bps (t −1.85), net −178.1.
  - Part of the long/short gap is the sample's positive average market drift, which flatters longs and penalizes shorts. The per-day IC is unaffected by it.
- **Delay buckets (166 days; E 288).**
  - Composite signed IC is between −0.0053 and −0.0038 in every bucket, all with |t| < 0.9.
  - The composite D10 − rest signed spread is +23.9 bps (t +2.10) in bucket A and +16.6 (t +1.27) in B. In C and D it is +2.1 and −4.4.
  - Top-decile net after costs is between −113 and −147 bps in every bucket.
- **Thesis subset (delay > 0.3, 166 days).**
  - Composite signed IC −0.0032 (t −0.65).
  - D10 − rest signed spread +21.8 bps (median +23.6, 59% of days positive, t +1.94).
  - Top-decile gross signed return +17.0 bps (t +1.47); **net −114.8 bps** (17% of days positive).
  - The gross spread is less than a fifth of the cost it would have to clear.
- **Reversal check.**
  - No whole-sample signed IC reaches t ≤ −2.0.
  - Two secondary cells do: return_mag in bucket A (−0.0145, t −2.04) and return_mag in the thesis subset (−0.0138, t −2.28). These overlap (A is most of the thesis subset), so they are not independent. They are two cells out of several dozen.
- **Pooled signed IC decomposition.**
  - Composite pooled signed IC −0.0052 = between-day −0.0017 + within-day −0.0035.
  - Unlike the raw case, there is no large between-day component to create a misleading pooled number.
  - Control on the same rows: raw pooled +0.0109 = between +0.0168 + within −0.0059. This is the same time-effect pattern found on 2026-09-28.
- **Matched vs unmatched rows (within the 288 days).**
  - Rows without a PERMNO (30,984, mostly OTC-ADR-like tickers) look like the used rows on composite (mean 0.495 vs 0.499, top-decile share 10.1% for both). Their delay is higher (mean 0.334 vs 0.286).
  - Rows that failed the identity check (1,540) and rows with no CRSP record (1,145) skew toward high composites (top-decile share 15.5% and 12.8%). They are too few to move a 952,002-row result.
  - Zero-return rows (12,502) have low composites (0.343; 0.2% in the top decile) and high delay, as expected for untraded or illiquid days.
  - The excluded Jan–Apr 2026 rows match the used rows on composite (0.497; 10.0% top decile). Nearly all of them have a delay score (97.8%), against 58.3% in the sample. So the delay-bucket results would have gained the most from those months.

## Caveats

- **Survivorship and universe bias.** These numbers still carry the survivorship prefilter (symbols chosen for being alive and liquid in April 2026) and the missing market-cap filter (roughly all liquid U.S. equities, not $500M–$2B small caps). For the earlier raw-return test those biases plausibly favored the strategy. **For a signed test, the direction of their effect is not known in advance.** Names that later collapsed are missing, and whether their moves, signed by day-t direction, would have raised or lowered these numbers can't be determined without the data.
- **Sample coverage.** The sample ends 2025-12-26 and excludes 74 of the 362 days, as well as most OTC-ADR-like and preferred tickers.
- **Cost assumptions.** Costs are the whitepaper's assumptions (120 bps long, 150 bps short), not measurements.
