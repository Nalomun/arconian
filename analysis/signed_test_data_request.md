# Signed test — Step 0 result: data not present; CRSP pull needed

2026-09-29. Status: **stopped at Step 0**. No test has been run, and no pre-registration has been written yet.

## 1. What the backtest CSV contains

`backtest_2024-01-01_2026-04-21.csv` (137,121,407 bytes; built from licensed Norgate data and is not distributed) has eight columns:

```
date, ticker, composite, volume, return_mag, sector_rs, delay, fwd_return_3d
```

None of them is a signed day-t return (close(t-1) → close(t)), and none lets you compute one exactly:

| column | why it doesn't work |
|---|---|
| `return_mag` | Percentile rank of \|day-t return\| against its own 60-day history (`scripts/backtest.py`, `compute_return_signal(abs(today_ret), …)`). The sign is discarded before ranking. |
| `sector_rs` | Signed, but it is a percentile rank of the *5-day* stock-minus-sector-ETF spread. It is neither a 1-day return nor absolute. |
| `fwd_return_3d` | close(t) → close(t+3). Overlapping 3-day windows don't identify single-day returns without three unknown starting values. That would be a reconstruction, which the task rules out. |
| `composite`, `volume`, `delay` | Unsigned by construction. |

Other places I checked:
- **The first commit of the private development repository.** `.cache/` holds 2,269 `ticker_info_*` JSONs, the EDGAR maps, and a 3-month sector-ETF price file. It has no stock price history for the window.
- **`audit_extreme_returns.csv`.** Only 12,950 extreme rows, and it holds forward returns only.
- **`arconian.db`.** Live scans only, from 2026-04-10 on, for about 323 names (PROJECT_HISTORY §3.10). It is a different population and a different period.

`backtest.py` computed `today_ret` in memory and never wrote it out. The Norgate price data that produced it can't be re-pulled either (PROJECT_HISTORY §3, R3).

## 2. What to pull from CRSP

**Return definition.** The backtest's `NorgateAdapter()` was built with its default, `price_adjustment="totalreturn"` (`data/norgate_adapter.py:52`). Day-over-day close ratios in that series are therefore total returns, and the CRSP match for them is **DLYRET**, which includes distributions. DLYRETX (ex-distributions) is pulled for reference only. The two differ in sign only on ex-dates with tiny price moves, and the script counts how often that happens. The backtest calendar was SPY trading days, which is the NYSE calendar that CRSP also uses.

**Tables (CRSP CIZ / "v2" format on WRDS):**

| table | fields | filter |
|---|---|---|
| `crsp.stksecurityinfohist` | `permno`, `secinfostartdt`, `secinfoenddt`, `ticker`, `tradingsymbol`, `shareclass` (plus `securitytype`, `sharetype`, `primaryexch`, `securitynm` for diagnosis) | records whose validity interval overlaps 2024-11-01 … 2026-04-21 |
| `crsp.stkdlysecuritydata` | `permno`, `dlycaldt`, `dlyret`, `dlyretx`, `dlyprc`, and if present `dlyprcflg`, `dlyprevdt`, `dlyretmissflg` | candidate PERMNOs; `dlycaldt` 2024-10-25 … 2026-04-24 |

**Date range.** The strict minimum is 2024-11-01 (first signal date) through 2026-04-20 (the last signal date, 2026-04-15, plus 3 trading days, needed only for the mapping check). The script pulls a few days of buffer on each side.

**Vintage matters.** The window runs to April 2026. As I understand it, the legacy SIZ tables (`crsp.dsf`, `crsp.stocknames`) stopped updating after the December 2024 release, which is why the script uses the CIZ tables. Annual-only CIZ subscriptions usually end at the prior December, so they may stop at 2025-12-31 and drop the last ~70 trading days. The script prints the latest `dlycaldt` it can see and warns if coverage falls short. I have not verified these table and column names against a live WRDS schema. The script checks them first and exits with the actual column list if they differ.

## 3. Ticker → PERMNO mapping

The CSV's tickers are Norgate symbols as of April 2026 (the survivorship prefilter kept only names alive then). The script maps them in four steps:

1. **Normalize.** Strip Norgate's delisted suffix (`HOLX-202604` → `HOLX`). For class shares (`BRK.B`), also try `BRKB`, `BRK/B`, `BRK B`, and root `BRK` + `shareclass` `B`. For Norgate's preferred form (`BAC-L`), try the usual preferred spellings.
2. **Find candidates.** Match against `ticker` or `tradingsymbol` on any name record overlapping the window. This catches names that changed ticker mid-window.
3. **Validate identity.** For each candidate PERMNO, compound DLYRET over the next 3 CRSP trading days and compare with the CSV's `fwd_return_3d`. Days a name didn't trade are forward-filled, the same way `backtest.py` looks up the last close on or before a date. Per ticker, keep the candidate with the highest match rate.
4. **Keep rows.** A row enters the signed test only if its own forward-return check agrees within **5 bps** (`FWD_TOL = 5e-4`, fixed before seeing any CRSP data). The script also reports how many rows agree within 1e-5 and 1e-4, so you can see how tight the fit actually is.

The forward-return comparison only confirms identity. The day-t return itself is DLYRET on date t, read directly, and nothing is inferred from forward returns.

**Expected coverage losses** (counted from the CSV):

| ticker form | tickers | rows | likely outcome |
|---|---|---|---|
| 5 characters ending in Y/F (`AAGIY`, `ADYEY` …; look like OTC ADRs) | 128 | 27,759 (2.15%) | Mostly absent from CRSP (NYSE/AMEX/Nasdaq/Arca only) |
| Preferred-style dash (`BAC-L`, `KKR-D` …) | 9 | 2,209 (0.17%) | Probably absent from CRSP |
| Class dot (`BRK.B`, `BF.B` …) | 11 | 3,507 (0.27%) | Handled |
| Norgate delisted suffix (`HOLX-202604` …) | 4 | 1,199 (0.09%) | Handled |

So about 2–3% of rows will likely drop, plus whatever falls outside your CRSP vintage. **Consequence for the design:** the signed test would run on a slightly different sample than the 2026-09-28 raw-return check. I'd propose that the pre-registration also re-run the raw-return per-day IC on the same validated subset, so any difference between raw and signed results can't come from the sample change. That's your call when we write Step 1.

## 4. The script

`scripts/research/crsp_pull_day_t_returns.py`

```
pip install wrds pyarrow
# needs backtest_2024-01-01_2026-04-21.csv at the repo root (built from licensed Norgate data and is not distributed)
python scripts/research/crsp_pull_day_t_returns.py --wrds-user YOUR_WRDS_USERNAME
```

It writes to `crsp_pull/`:
- `day_t_returns.parquet`: one row per CSV row, with `permno`, `ret_t` (DLYRET), `retx_t`, price flag, `fwd3_crsp`, `fwd3_absdiff`, `fwd3_match`, and `map_status` ∈ {`ok`, `fwd_mismatch`, `no_crsp_row_or_ret`, `no_permno`}.
- `ticker_map.csv`: per ticker, the candidate PERMNOs, the chosen PERMNO, and its match rate.
- `pull_report.txt`: coverage, the distribution of forward-return differences, the count of exactly-zero day-t returns, sign disagreements between DLYRET and DLYRETX, and match rate by ticker form.

`crsp_pull/` is **not gitignored**. It holds CRSP-licensed data, so don't commit it (or add `crsp_pull/` to `.gitignore`).

Testing so far: I ran the script end to end against a mock WRDS connection with synthetic returns, which exercised symbol normalization, candidate matching, validation, and the report. It has **not** run against real WRDS: the `wrds` package isn't installed here and I have no credentials. If it fails on the schema check or anything else, send me the output.

## 5. Next

Once `crsp_pull/pull_report.txt` exists, send it to me. Then comes Step 1: write and commit `analysis/signed_test_preregistration.md` before any test runs. It will name DLYRET as the day-t return, the 5 bps identity tolerance, the validated-row sample, and the count of exactly-zero rows dropped. Step 2 runs after that commit.
