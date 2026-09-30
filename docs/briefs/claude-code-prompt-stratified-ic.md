# Task: Stratified IC Analysis on Backtest Results

## Context

Arconian's first historical backtest (`scripts/backtest.py`) just completed. Output: `backtest_2024-01-01_2026-04-21.csv` (~138 MB, 1.29M rows with valid forward returns) at the project root.

Aggregate IC results were disappointing — composite IC = +0.013, all signals below the whitepaper's 0.02 threshold (§7.4), and delay had a *negative* IC of −0.024 (wrong sign).

But the backtest measures aggregate IC across the entire filterable universe (~3,000–4,000 tickers per month), not the top of the distribution that the whitepaper actually targets. The thesis (§1.5, §3.3, §4.2) is about high-composite events in high-delay names — not the whole universe averaged together. We need stratified analysis to determine whether the thesis is dead or just unmeasured before deciding next steps (rebuild backtest with directional confirmation/earnings exclusion, or proceed straight to Phase 1 paper trading per §11).

This is a **read-only analysis task**. Do not modify the backtest script, the database, or any other files in the project. Only read the CSV and write outputs.

## What to build

Create `scripts/analyze_backtest.py` that loads `backtest_2024-01-01_2026-04-21.csv` and produces the analyses below. The script should print a clean summary to stdout AND write a structured report to `analysis_2026-04-21.md` at the project root.

### Analysis 1: Composite score decile lift

The whitepaper's tiered risk system (§4.2) keys off the 70th, 85th, and 95th composite percentiles. Aggregate IC tells us nothing about whether the top deciles separate from the bottom.

For each decile (1 = lowest composite, 10 = highest), compute:
- n observations
- Mean 3-day forward return (in bps)
- Median 3-day forward return (in bps)
- Hit rate (% of observations with fwd_return_3d > 0)
- Standard error of the mean

Present as a table. Then compute and report:
- **Top-decile minus bottom-decile mean return** (the "long-short spread"). This is the headline number for whether the composite has economic meaning.
- **Top-decile mean return vs. 120 bps cost threshold** (§5). Does the top decile clear costs?
- **Monotonicity check:** does the mean return increase monotonically with decile, or is it noisy? Report Spearman rank correlation between decile and mean return.

### Analysis 2: IC segmented by delay bucket

The most diagnostically interesting result was delay's negative IC. The whitepaper treats delay as a structural filter (§2.3: Delay > 0.3 = primary targets) and a scoring component (§3.2.5), not as a standalone linear predictor. Negative aggregate IC may mean the thesis is dead, OR it may mean delay-as-a-filter still works while delay-as-a-linear-signal doesn't.

Bucket observations by delay score:
- Bucket A: delay > 0.5 (very high delay — strongest thesis fit)
- Bucket B: 0.3 < delay ≤ 0.5 (high delay — primary targets per §2.3)
- Bucket C: 0.1 < delay ≤ 0.3 (low delay — scoring penalty applies)
- Bucket D: delay ≤ 0.1 (very low delay — thesis does not apply)
- Bucket E: delay is NULL (insufficient history for estimation)

For each bucket, compute:
- n observations
- IC of composite vs. fwd_return_3d (Spearman)
- IC of volume, return_mag, sector_rs vs. fwd_return_3d
- p-value for each IC
- Mean fwd_return_3d in the top composite decile *within the bucket*

The critical question: **does the composite IC in Bucket A or B clear the 0.02 threshold?** If yes, the thesis lives but needs the delay filter to express itself. If no, the thesis is in trouble even on its preferred subset.

### Analysis 3: Joint stratification — top decile × high delay

This is the most direct test of the actual trading hypothesis. Filter observations to:
- Top composite decile (10th decile from Analysis 1)
- AND delay > 0.3 (primary target per §2.3)

For this filtered subset, report:
- n observations
- Mean fwd_return_3d in bps
- Median fwd_return_3d in bps
- Hit rate
- Standard error
- **Mean fwd_return_3d minus 120 bps cost (§5)** — the after-cost expected return per signal
- Distribution of forward returns: 5th, 25th, 50th, 75th, 95th percentiles

Also report the same metrics for: top decile across all delay buckets (no delay filter), and high-delay across all deciles (no composite filter), so we can see which filter is doing the work.

### Analysis 4: Time stability

Whitepaper §1.4 acknowledges 18–24 month strategy lifespans. If the signal is decaying through the backtest window, that's important context.

Split the sample into thirds by date (early / middle / late). For each third, recompute:
- Composite IC
- Top-decile mean fwd_return_3d
- Top-decile × delay > 0.3 mean fwd_return_3d

Is there a trend? A signal that was strong in early-2024 and dead by late-2025 tells a very different story from a signal that's been weak the whole time.

### Analysis 5: Sanity checks

Print:
- Total rows, rows with non-null composite, rows with non-null delay
- Distribution of composite scores (5/25/50/75/95 percentiles) — should span roughly 0 to 1
- Distribution of delay scores (same percentiles, plus % null)
- Number of unique tickers
- Date range covered
- Any obvious data quality issues (e.g., extreme forward returns suggesting splits not adjusted, all-null columns, etc.)

## Implementation notes

- The CSV has columns: `date, ticker, composite, volume, return_mag, sector_rs, delay, fwd_return_3d`. Confirm this on load and fail loudly if schema differs.
- 138 MB CSV — load with pandas, but be mindful of memory. Use `dtype` hints (float32 for signal columns) if helpful. `pd.read_csv` should be fine on any modern machine; no need for chunking.
- Use `scipy.stats.spearmanr` for IC, consistent with the existing backtest.
- For deciles, use `pd.qcut(..., q=10, labels=False, duplicates='drop')`. Handle ties gracefully — composite scores may have repeated values.
- All return numbers should be reported in **basis points** (multiply fwd_return_3d by 10000) for readability.
- Round IC to 4 decimals, returns to 1 decimal (in bps), p-values to 4 decimals.
- Do not place any trades, do not modify the database, do not modify the backtest script. Read-only analysis.
- The whitepaper is at `arconian-v3.md` in the project root if you need to reference it. Do not paraphrase the whitepaper into the report — just cite the relevant section number where conclusions hinge on it.

## Output format

Console output: print each analysis section with a clear header, the requested table or numbers, and a one-sentence interpretation flagging whether the result clears the relevant whitepaper threshold (IC > 0.02 for §7.4, mean return > 120 bps for §5).

Markdown report `analysis_2026-04-21.md`: same content as console but formatted for human review and committing to the repo. Include a short "headline findings" section at the top (3–5 bullet points) covering: (1) does the composite have decile lift, (2) does the thesis live in any delay bucket, (3) does the joint top-decile × high-delay subset clear costs, (4) is the signal time-stable. End with a short "what this means for next steps" section that does NOT make the go/no-go call itself but lays out what each combination of findings would imply (e.g., "if Analysis 3 shows >40 bps after costs and Analysis 4 is stable, this supports building the missing earnings/directional layers and rerunning before Phase 1").

## Don't

- Don't refactor or modify `scripts/backtest.py` or anything in `signals/`, `universe/`, `risk/`, `data/`, `journal/`, or `execution/`.
- Don't make a trading decision or run the seed_universe script or anything else.
- Don't add new dependencies — pandas, numpy, scipy are already installed.
- Don't write a long preamble in the report explaining the whitepaper. Cite section numbers; the reader has the whitepaper.
- Don't smooth over weird results. If Bucket A has n=200 and an IC of +0.15, report it AND flag that n is small enough to be unreliable. If something looks like a data quality issue, say so.
