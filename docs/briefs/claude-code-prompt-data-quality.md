# Claude Code Handoff: Two Data-Quality Bugs in Arconian

This is a handoff brief from a previous Claude Code session that ran on
2026-04-30. Read it end-to-end before doing anything. The previous session
fixed the runtime bugs (system not running, no trades opening, maintenance
no-op) and is now too long to safely continue. You're inheriting a clean
problem with concrete evidence.

## Project orientation (read these first, in order)

1. `CONTEXT.md` — current project context. The "2026-04-30 Session Notes"
   section at the top describes what the previous session diagnosed and
   fixed. The "Current Operational State" section describes how the system
   currently runs.
2. `arconian-v3.md` — the governing whitepaper. Sections you'll need:
   - **§3.2.6 Retail Attention Penalty** — exactly how `retail_attention_score`
     is supposed to be computed (StockTwits + Reddit + scanner flag + SI proxy).
   - **§3.3 Composite Score** — how `retail_attention_score` is consumed.
   - **§7.3 Feature Versioning** — why `delay_score_as_of_signal` exists and
     why it must NOT be the current value.
   - **§7.1 signal_log Schema** — every column.
3. `arconian-supplementary-docs.md` — Document 1 covers the StockTwits and
   Reddit API contracts (rate limits, fallback behavior). Doc 3 §3.3 covers
   NULL propagation rules.

You do not need to read more than the sections referenced above.

## The two bugs (both confirmed)

A read-only survey of the live `arconian.db` (run via
`python scripts/survey_state.py`) showed:

```
Component availability (non-NULL):
  volume_pctile_60d               1656  (100.0%)
  return_pctile_60d               1656  (100.0%)
  options_composite               157   (9.5%)
  sector_rs_percentile            1656  (100.0%)
  delay_score_as_of_signal        0     (0.0%)   <-- BUG 1
  retail_attention_score          0     (0.0%)   <-- BUG 2
```

`signal_log` has 1,656 rows spanning 2026-04-10 to 2026-04-23, all
`scan_type='open'`. Both bugs are systemic — not a partial failure on some
rows but **NULL on every single row** since launch. So the bugs are in the
write path, not in the data sources.

### Bug 1: `delay_score_as_of_signal` never populated

Per §7.3, this column must store the delay score that was active at the
time of the signal event, NOT the retrospectively re-estimated value. It
prevents temporal leakage when this data is later used for ML training
(§8.3). Without it, every future ML pipeline trained on this data will
silently leak future delay updates backward. The longer the bug runs, the
more corrupt training data accumulates — fix this before the scheduler
fix lets the system produce many more rows.

What's known:
- `universe_state.delay_score` is populated (the universe_manager runs
  `delay_score.compute()` monthly). So the source of truth exists.
- The signal_log column `delay_score_as_of_signal` is the destination.
- Something in the path between `signals/signal_engine.py` and the
  signal_log INSERT is dropping it on the floor (or never setting it).

Probable file scope:
- `signals/signal_engine.py` — the orchestrator that builds each
  SignalLog row.
- `journal/signal_log.py` — write helpers (if signal_engine uses them).
- `models.py` line ~326 onwards — the SignalLog SQLAlchemy model.
- `universe/delay_score.py` — verify the compute helper still returns
  values when called from the signal-engine path (it works in the universe
  manager path, so probably fine).

### Bug 2: `retail_attention_score` never populated

Per §3.2.6, `retail_attention_score` is the penalty input computed as
0.5×social_velocity + 0.3×scanner_flag + 0.2×short_interest_crowd. The
penalty is applied multiplicatively to the composite score (§3.3:
`composite_score = composite_score_raw × (1 - penalty_weight × retail_attention_score)`).

What's known:
- The composite scores in signal_log have a healthy distribution (median
  0.45, p95 0.82, max 0.97). They're not all-zero, which means
  `composite_score_raw` is being computed and is making it through — but
  the retail penalty is silently zero.
- `signals/retail_attention.py` exists. We don't know yet if it's invoked
  at all, if it returns 0 because the data adapters return zeros, or if
  it returns a value that's then dropped before the signal_log INSERT.
- `data/social_adapter.py` exists. If StockTwits/Reddit APIs are returning
  empty results, that explains `social_velocity=0`. CONTEXT.md "Open
  Questions" section flags this as unverified — confirming it is part of
  this work.

Probable file scope:
- `signals/retail_attention.py` — the computation (§3.2.6).
- `signals/signal_engine.py` — does it import and invoke retail_attention?
- `data/social_adapter.py` — are the StockTwits + Reddit adapters
  returning meaningful values, or zeros / Nones?
- `journal/signal_log.py` and `models.py` — is the column being written?

## Hard constraints

- **The system is running.** PID 17384 (or successor — `Get-CimInstance
  Win32_Process | where { $_.CommandLine -like '*main.py*' }` will tell
  you). The scheduler will fire scans at 09:35, 12:00, 15:30 ET. Don't
  restart it casually. If you need to restart for a fix, follow the same
  procedure the previous session used: `Stop-ScheduledTask -TaskName
  Arconian` → wait → kill any straggling python processes via
  `Stop-Process -Force` → `Start-ScheduledTask -TaskName Arconian` →
  verify a single fresh PID via the same Get-CimInstance check, then tail
  `logs/arconian_stdout.log`.
- **Do not touch the four fixes from 2026-04-30.** Specifically:
  - `scripts/arconian_task.xml`, `scripts/setup_task_scheduler.ps1`
  - `execution/scan_scheduler.py` (misfire grace, coalesce)
  - `execution/order_manager.py:_fetch_entry_price` (two-stage fallback)
  - `journal/maintenance.py`, `main.py:_do_maintenance` (wiring)
- **Do not try to fix the 4 pre-existing test failures.** The 2 deadman
  timer failures and the 2 universe_manager UTC-midnight flakes are
  unrelated and tracked.
- **Do not refactor unrelated code.** If you find dead code or odd
  patterns, leave them. Scope is narrow: get those two columns populated
  on every new signal_log row.
- **Don't write new docs.** Notes go in inline code comments only when
  the *why* is non-obvious.

## Acceptance criteria

A fresh signal scan triggered after your fix produces signal_log rows
where:

1. `delay_score_as_of_signal` is populated for every ACTIVE-state ticker
   that has a computed delay score in `universe_state` (which should be
   most of them — 326 tickers tracked, 322 ACTIVE).
2. `retail_attention_score` is populated (in [0, 1]) on every row,
   reflecting the formula in §3.2.6. If a sub-component data source
   (StockTwits, Reddit, scanner flag) is unavailable, that sub-component
   defaults to 0 per §3.3 NULL propagation rules — but the overall
   `retail_attention_score` field still gets a numeric value, NOT NULL.

You can validate by:
- `python scripts/survey_state.py` — re-run after a fresh scan and check
  the "Component availability" section for non-zero counts on both
  columns.
- Run a manual one-off scan: `python main.py --dry-run` (per main.py
  module docstring it runs one scan and exits — does not interfere with
  the running daemon).
- Or inspect a fresh row directly via sqlite3 / a small read-only Python
  script.

Existing 1,656 rows do not need backfilling. They're a known-bad cohort
documented in `project_current_status.md` memory; future ML pipelines
will need to filter them out anyway.

## Tests

- Add unit tests for whatever code path you change. Match the conventions
  in `tests/test_order_manager.py` and `tests/test_maintenance.py`
  (pytest, MagicMock, isolated_db fixture for DB-touching tests).
- Run `python -m pytest tests/ -q` before declaring done. Expect 4
  pre-existing failures (2 deadman, 2 universe-manager UTC flake);
  everything else should pass.
- If you can't add a test that proves the fix end-to-end, write a smoke
  script under `scripts/` (like `scripts/verify_entry_price_fix.py`) that
  exercises the path live and confirms the field is populated.

## Tools at your disposal

- `scripts/survey_state.py` — read-only audit. Run before and after to
  measure progress.
- The schemas in `models.py` are authoritative.
- The whitepaper sections referenced above are authoritative on intent.
- The `arconian.db` file is WAL-mode SQLite; you can open it read-only
  via `sqlite3.connect("file:arconian.db?mode=ro", uri=True)` from
  Python while the daemon writes.

## What "done" looks like

A short report back to the user covering, per bug:
1. Root cause (one or two sentences)
2. The fix (file + line range)
3. Test that locks it in
4. Smoke evidence: a fresh signal_log row showing the column populated

Two bugs, two short reports. No need to write a separate doc — message
back inline.

## What's NOT in scope

- The 4 pre-existing test failures.
- Anything in the Zinniinae UI work (separate session, separate repo).
- The auto-optimization / weight-modulation discussion (deferred per
  whitepaper §3.3 IC recalibration cadence — needs 60+ trading days of
  labeled data first).
- Backfilling the existing 1,656 rows.
- Any "while you're in there" cleanup of the signal-engine code.

Good luck. Ping the user with progress at the natural checkpoints, not
with running commentary.
