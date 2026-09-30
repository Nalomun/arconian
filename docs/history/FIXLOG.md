# Arconian — Fix Log

Working record of the remediation effort that began **2026-06-23**, following the
`DIAGNOSIS.md` audit (2026-06-10, 36 findings / 6 blockers). This file is the
durable companion to `DIAGNOSIS.md`: DIAGNOSIS says *what is wrong*, FIXLOG says
*what we decided and what we changed*.

Baseline at start: daemon stopped since 2026-06-09; no scan data since 2026-05-12;
test suite 361 passed / 2 failed.

---

## Decisions (approved by operator 2026-06-23)

| # | Decision | Resolution |
|---|----------|-----------|
| 1 | Deadman scope in paper mode | **Alert-only.** Stages 2/3 send CRITICAL alerts but perform no DB writes. Config-gated (`deadman.action_mode`, default `alert_only`); `full` reserved for Phase 4+ live capital. |
| 2 | Risk engine in auto-trade | **Minimal cap now, full engine later.** Add per-ticker dedup + `max_concurrent_positions` cap to `auto_paper_trade_candidates` (no pyramiding). Full `RiskEngine` wiring deferred to Tier 3. |
| 3 | May 6–12 paper-trade dataset | **Known-bad; restart the clock.** Mark the 2026-05-06→05-12 cohort bad (alongside the April cohort) and restart paper-trade evaluation after fixes soak. |
| 4 | Retail penalty weight | **0.10** (match whitepaper, was 0.20 in config). Revisit with live IC data. |
| 5 | Off-hours auto-trades | **Skip by design.** Do not open paper trades from scans that run outside market hours (stale EOD fills bias expectancy). Signal rows still logged. Simplifies the F-7 fix. |
| 6 | Options sub-signal | **Suppress now, build store in Phase 2.** Stop the constant-0.5 distortion: options returns NULL when it lacks real IV/PC history, so existing weight-renormalization redistributes its 15% across the four working signals. Build the IV/PC history store as a Phase-2 project. |

---

## Fix sequence & progress

Tiers follow DIAGNOSIS §5. Status: ✅ done · 🔶 in progress · ⬜ pending.

### Tier 1 — stop the bleeding

| Fix | Findings | Status |
|-----|----------|--------|
| Defang deadman (alert-only, sticky halt, lazy-arm, 20h timeout) | F-1, F-2 | ✅ |
| Fix `good_friday` month-rollover time-bomb | F-12 | ✅ |
| Close config-validation gaps for auto-trade keys | F-27 | ✅ (folded into cluster A) |
| Auto-trade: ticker dedup + max_concurrent cap | F-3 (minimal) | ✅ |
| Skip off-hours auto-trade candidates | decision #5, F-7 | ✅ |
| Wire `EarningsCalendar` + trading-day windows | F-5, F-15 | ✅ |
| Sector-RS close/ETF alignment | F-8 | ✅ |
| Per-ticker exception isolation + OHLCV alignment | F-9 | ✅ |
| Outcome labeling: stop price, 4-bar gate, day-0 MAE/MFE | F-6, F-11 | ✅ |
| Config: retail weight 0.10 + options suppression | decisions #4, #6, F-4 | ✅ |

### Tier 2 — complete
All actionable Tier-2 findings are done (cost validator F-10, outcome dead-letter
F-24, history cliff F-20, universe isolation F-14, Schwab 401 F-13, parameter
baseline F-23, IC segment/direction F-25, latent EDGAR/macro/social/Schwab bugs
F-16/F-35/F-36). Only F-18 (Pass-2 volumes → directional confirmation) remains
and is moot while `options_enabled=false`.

### Tier 3 — structural (complete)
- ✅ **F-17** risk-layer math bugs fixed in dead code (prerequisite to wiring).
- ✅ **#18** real risk layer wired into `auto_paper_trade_candidates` (off/shadow/
  enforce, default off) + regime inputs + CB persistence + CB feeding.
- ✅ **F-19** entry-day bar evaluated pessimistically (adverse breach only).
- ✅ **F-26** early-close calendar + single-worker job serialization.
- ✅ **F-33** `datetime.utcnow()` → `timeutils.utcnow()` (naive UTC) everywhere.
- ✅ **#18 follow-on** — `sub_industry` + 60d returns enriched, so **all** engine
  constraints (regime, CB, max-positions, sub-industry, sector-capital,
  correlation-cluster, overnight) are active in shadow/enforce.

**Deferred to Phase 2 (not remediation):**
- IV/PC history store to re-enable `options_enabled` (decision #6 — a Phase-2
  project); **F-18** Pass-2 volumes → directional confirmation (moot while
  options off).

---

## Change log

### 2026-06-23 — Cluster A: deadman + scheduler time-bomb + config validation

**Files:** `execution/deadman_switch.py`, `execution/scan_scheduler.py`,
`config/arconian_config.yaml`, `config/config_loader.py`, `main.py`,
`tests/test_scan_scheduler.py`.

- **F-1/F-2 (deadman):**
  - Added `action_mode` (`alert_only` default). `_widen_stops` and
    `_force_close_all` now early-return with a CRITICAL alert and **zero DB
    mutation** unless `action_mode == "full"`. This is what destroyed the
    2026-05 dataset; it can no longer write NULL-P&L force-closes in paper mode.
  - **Sticky halt:** `heartbeat()` no longer clears `_halted`. Added `reset()`
    for explicit operator-acknowledged re-arm. (`is_halted` is read nowhere in
    the production scan path — verified — so this is purely informational/safe.)
  - **Timeout cadence:** `scan_timeout_minutes` 60 → 1200 (20h) in config, so the
    watchdog can't fire between legitimate scans (intraday gaps up to 210 min;
    overnight ~18h). Documented the trade-off (coarse backstop, not per-scan).
  - The lazy-arm refactor (arm on first scan via `ensure_armed`, not at init) was
    already present in the code; updated the two stale tests (F-31) that still
    encoded the arm-at-init contract, plus the heartbeat-clears-halt test.
- **F-12 (good_friday):** compute via `easter - timedelta(days=2)` instead of
  `date(year, easter.month, easter.day - 2)`, which raised `ValueError` whenever
  Easter fell on Apr 1–2 (e.g. 2029) and would have aborted every scheduled job
  that whole year. Added regression tests for 2025 and 2029.
- **F-27 (config validation):** added `execution.auto_paper_trade`,
  `auto_paper_trade_min_score`, `account_equity`, and `deadman.action_mode` to
  `_REQUIRED_KEYS` so the params that control live auto-trading fail loudly if
  deleted (no more silent fallback to dataclass defaults).

**Verified:** full suite **366 passed, 0 failed** (was 361/2-failed). Config
loads with new keys (`action_mode=alert_only`, `scan_timeout_minutes=1200`,
`account_equity=26000`). `good_friday(2029) == 2029-03-30` confirmed empirically.

### 2026-06-23 — Cluster B: auto-trade gating (dedup, cap, off-hours)

**Files:** `execution/order_manager.py`, `tests/test_order_manager.py`.

- **F-3 (minimal cap, decision #2):** `auto_paper_trade_candidates` now snapshots
  open positions and (a) skips any ticker already held — no pyramiding, covering
  both pre-existing trades and earlier opens within the same batch — and (b)
  stops opening once open positions reach `risk.max_concurrent_positions`
  (candidates are taken best-first; remaining count reported as `capped`). The
  full RiskEngine (regime/sector/correlation/circuit-breakers) is still deferred
  to Tier 3 per decision #2.
- **Decision #5 (off-hours):** added an injectable `now_et` param and a
  `_within_market_hours` guard (RTH 09:30–16:00 ET on a session day). Off-hours
  batches open nothing and return `off_hours=True`; signal rows remain logged.
  The scheduled scans (09:35/12:00/15:30) are all inside the window.
- **F-7 (incidental):** hardened the entry-price history fallback against
  yfinance-1.3 MultiIndex columns + pandas-3 `float(Series)` removal. Note this
  fallback is no longer used to *open* off-hours trades (decision #5), but it is
  still reachable intraday when `get_latest_price` returns None for a thin name,
  so the crash fix matters.
- Added `TestAutoPaperTradeGating` (6 tests) + a MultiIndex fallback regression.
  `auto_paper_trade_candidates` previously had **zero** test coverage.

**Verified:** full suite **373 passed, 0 failed**.

### 2026-06-23 — Cluster C: signal engine + earnings wiring + weights

**Files:** `signals/signal_engine.py`, `data/earnings_calendar.py`, `main.py`,
`config/arconian_config.yaml`, `config/config_loader.py`,
`tests/test_signal_engine.py`, `tests/test_earnings_calendar.py` (new).

- **F-5 (earnings unwired):** `main.py` now constructs `EarningsCalendar` and
  passes it to `SignalEngine`. Earnings exclusion actually fires for the first
  time in production.
- **F-15 (calendar vs trading days):** added `trading_days_to_next_earnings` /
  `trading_days_since_last_earnings` (via `np.busday_count`) and switched
  `_check_earnings` to gate on trading-day distance. A Friday reporter is now
  correctly excluded on the following Monday (1 trading day, not 3 calendar).
- **F-8 (sector-RS):** tail-align the stock (6mo) and ETF (3mo) close series
  (`[-min_len:]`) so the spread history pairs the same recent sessions, instead
  of the oldest stock days against the newest ETF days.
- **F-9 (isolation + alignment):** wrapped the Pass-1 per-ticker call in
  try/except (one bad ticker can't abort the scan; it emits an
  `exclusion_reason='scoring_error'` row). Rewrote `_extract_ohlcv` to assemble
  OHLCV into one frame and `dropna()` jointly, so all returned arrays are
  equal-length and date-aligned (kills the silent dollar-volume/ATR corruption).
- **Decision #6 / F-4 (options):** added `signal.options_enabled` (default
  **false**). Pass-2 options enrichment is gated on it; while off,
  `options_composite` stays NULL and the existing renormalisation redistributes
  its 15% across the four working signals (no more distorting constant-0.5).
  Re-enable only after the Phase-2 IV/PC history store exists.
- **Decision #4:** `retail_penalty_weight` 0.20 → 0.10 (whitepaper).
- **F-29:** removed the dead `si_pct` assignment in Pass 1.
- **F-27 (cont.):** `signal.options_enabled` added to required config keys.
- Tests: new `test_earnings_calendar.py` (8) + `TestEarningsExclusion` (5) in
  `test_signal_engine.py`.

**Verified:** full suite **386 passed, 0 failed**.

### 2026-06-23 — Cluster D: outcome labeling (F-6, F-11)

**Files:** `journal/outcome_collector.py`, `tests/test_journal.py`.

- **F-6 (stop barrier):** `_process_event` no longer passes `event.entry_price`
  (the fill price) as the stop barrier. For traded rows it loads the linked
  `Trade.stop_price` (via `trade_id`); untraded rows keep `stop_price=None` →
  ATR-proxy barrier. Previously any post-entry bar touching the entry price
  labeled the row −1 regardless of the real outcome.
- **F-11 (premature labeling):** require the full 4-bar window (day 0–3) before
  labeling; fewer bars defer and retry later, instead of writing premature
  time-barrier 0s and day-early excursions for Thu/Fri signals.
- **F-11 (excursions):** MAE/MFE now measured over days 1–3 only (entry proxy is
  the day-0 close), matching the barrier loop — no more day-0 intraday spikes
  inflating MFE.
- Tests: new `TestProcessEvent` (4) — the F-6 test labels +1 only with the real
  stop (would be −1 under the old bug); the MFE test rejects a day-0 spike.

**Verified:** full suite **390 passed, 0 failed**.

Note: the permanently-unresolvable-row queue clog (F-24) is **not** addressed
here — it's a Tier-2 dead-letter task. The 4-bar gate defers such rows but does
not yet expire them.

### 2026-06-23 — Cluster E: repo cleanup (F-32)

**Files:** `.gitignore` (+ `git rm --cached`).

- Untracked the **2,272 `.cache/` JSON files** (runtime API cache that showed as
  perpetually-modified) and the **two generated CSVs** (the 132MB
  `backtest_*.csv` and 1.2MB `audit_extreme_returns.csv`). All files remain on
  disk — only removed from the git index — and are now gitignored.
- Verified secrets were already safe: `.env`, `schwab_token.json`, and `*.db`
  are gitignored and were never tracked.
- **Not committed.** The untracking is staged (2,274 deletions) alongside the
  source fixes; left for operator review. Windows artifacts (`*.bat`, the Task
  Scheduler `.ps1`/`.xml`) were left in place — small and still referenced by
  CONTEXT.md; revisit if the project stays on Linux.

---

### 2026-06-24 — Cluster F (Tier 2): cost validator + outcome dead-letter

**Files:** `journal/cost_validator.py`, `journal/signal_log.py`,
`journal/outcome_collector.py`, `tests/test_journal.py`.

- **F-10 (cost validator):** `_decompose_cost` no longer writes
  `abs(round-trip P&L)` into the cost columns (a 3% mover recorded "300 bps of
  slippage"). It now returns a coherent **modeled** breakdown whose components
  sum to the total (`spread + impact + adverse = total`). Documented that this
  is a model, not a measurement — real slippage needs an intended-vs-actual fill
  pair that paper trading doesn't produce (Phase 4). Kills the permanent
  false-alarm exceedance flag.
- **F-24 (dead-letter):** `get_unresolved_events` gained a `max_age_days` upper
  bound (default 30) so rows that can never resolve (delisted/halted → never 4
  bars, made worse by the F-11 gate) age out of the oldest-first queue instead
  of clogging its head forever. Added `count_abandoned_events` + a WARNING in
  `outcome_collector.run`, and a warning when the signal-day bar is missing and
  the window silently shifts.
- Tests: +4 (`TestOutcomeQueueDeadLetter`, cost-components-sum).

**Verified:** full suite **394 passed, 0 failed**.

---

### 2026-06-24 — Cluster G (Tier 2): universe isolation + Schwab 401

**Files:** `universe/universe_manager.py`, `universe/universe_state.py`,
`data/schwab_adapter.py`, `tests/test_universe_manager.py`,
`tests/test_schwab_adapter.py`.

- **F-14 (universe daily-check isolation):** `run_daily_check` previously held
  **one** open write transaction across every per-ticker yfinance/Schwab call,
  so (a) a multi-minute SQLite write lock blocked the live scheduler and (b) one
  raising ticker rolled back the whole day's state-machine progress. Restructured
  to: snapshot the working set in a short read txn → fetch filter data **outside**
  any transaction → apply each transition in a short **per-ticker** write txn,
  each wrapped in try/except (one bad ticker → `"error"`, others unaffected).
  `_process_ticker` no longer does network I/O (takes a precomputed `result` +
  `history_days`).
- **F-14 (outage handling):** added `FilterCheckResult.is_inconclusive` (every
  check `None`). `all_pass` returns True for an all-None row, so a **total data
  outage** was silently counting as a clean pass — resetting fail streaks and
  advancing reactivation hysteresis, which could reactivate a genuinely-suspended
  ticker. `_process_ticker` now returns `"inconclusive"` with **no** counter/state
  change on an outage day. Date-based SUSPENDED→REMOVED still fires (time-based,
  not data-based). *(Monthly refresh shares the long-transaction shape but is out
  of F-14's stated scope — runs monthly/off-hours; left as-is.)*
- **F-13 (Schwab 401):** a dead 7-day refresh token made every call 401 → retried
  3× with 10 s sleeps → None, indistinguishable from a blip (~13+ min wasted per
  post-expiry scan, all-None data, no alert). Added a sticky `_auth_failed` flag
  (+ `auth_failed` property): the first 401 in any of the four API methods flips
  it via `_note_auth_failure`, which fires **one** CRITICAL Telegram alert and
  returns immediately (no retry, no backoff). Every later Schwab call then
  short-circuits at method entry — no network, no rate-limit wait — for the rest
  of the session (reset only by reconstructing the adapter after reauth).
  `get_account_info` (the token heartbeat) routes through the same path. 5xx
  behaviour unchanged.
- Tests: +12 (`TestDailyCheckOutageAndIsolation` + `is_inconclusive` units;
  `TestAuthFailureHandling`).

**Verified:** full suite **406 passed, 0 failed**.

---

### 2026-06-24 — Cluster H (Tier 2): IC analytics correctness

**Files:** `journal/parameter_tracker.py`, `journal/ic_tracker.py`,
`journal/signal_log.py`, `tests/test_journal.py`.

- **F-23 (parameter baseline):** `record_config_snapshot` diffed against the
  most recent `triggered_by='startup'` row, which after any real change is a
  per-key **change row** whose `new_value` is a scalar — so the next startup
  diffed the full config against a float and wrote a garbage `parameter_path=''`
  row (and re-diffed everything). Now selects the diff base by
  `parameter_path='_baseline'` (full-config snapshot rows only, id-tie-broken)
  and **refreshes the full-config baseline every startup**; re-baselines instead
  of silently skipping if a stored baseline is unparseable.
- **F-25 (IC segment):** the segment named `pre_earnings` actually filtered
  `earnings_proximity_tag='post_earnings_drift'` (no row is ever tagged
  `pre_earnings`). Renamed the segment to `post_earnings_drift` in both
  `ic_tracker._SEGMENTS` and the `signal_log` filter so ic_history rows match
  their names.
- **F-25 (IC direction):** IC correlated scores against raw long-convention
  `return_3d`, so a correct **bearish** call (high score, price fell) pulled IC
  toward zero. `_extract_scores_returns` now sign-flips `return_3d` for
  `direction_signal='bearish'` rows; bullish/ambiguous/NULL keep raw long.
  *(Operator deferred the convention; signed IC is the correct predictive-skill
  measure for a long/short book.)*
- Tests: +3 (change-then-restart never self-corrupts; `post_earnings_drift`
  segment; bearish sign-flip).

**Verified:** full suite **409 passed, 0 failed**.

---

### 2026-06-24 — Cluster I (Tier 2): latent data-adapter bugs (F-16/F-35/F-36)

**Files:** `data/edgar_adapter.py`, `data/macro_calendar.py`,
`data/social_adapter.py`, `data/schwab_adapter.py`, plus
`tests/test_edgar_adapter.py`, `tests/test_macro_calendar.py` (new),
`tests/test_social_adapter.py` (new), `tests/test_schwab_adapter.py`.

All three modules are unwired today; these make them safe to wire.

- **F-16 (EDGAR):** Item 1.01 ("Material Definitive Agreement", routine) was
  tagged `merger_acquisition`/999d → near-permanent suspension on ordinary 8-Ks;
  now `material_agreement`/5d. Item 8.01 ("Other Events") was `ticker_change` →
  now `other_events`/1d. Added the real merger item **2.01** and name/ticker
  changes under **5.03**. Patterns severity-ordered (first-match-per-8-K).
- **F-16 (macro):** June 2026 FOMC corrected **Jun 10 → Jun 17**; unknown years
  now WARN once instead of silently returning None (blackout fails open in
  2027+).
- **F-35 (social):** a failed/429/blocked StockTwits fetch cached 0 mentions for
  an hour and reported `stocktwits_available=True` (fail-open on meme names).
  Split `_fetch_stocktwits_mentions → (count, available)`: transient failures
  aren't cached and report `available=False`; genuine empty/404 stays available.
- **F-36 (Schwab):** `_parse_option_chain` now excludes far-dated LEAPS
  (`_within_dte`, ≤60 DTE) so OI/volume reflect near-term flow; documented that
  Schwab IV is **percent** while `compute_options_signal` expects decimal (the
  armed 100× unit bug).
- Tests: +17.

**Verified:** full suite **426 passed, 0 failed**.

---

### 2026-06-24 — Cluster J (Tier 3): risk-layer math bugs (F-17)

**Files:** `risk/cvar.py`, `risk/circuit_breakers.py`, `risk/risk_engine.py`,
`tests/test_risk_engine.py`, `tests/test_risk_cvar.py` (new),
`tests/test_circuit_breakers.py` (new).

Prerequisite to wiring the risk layer (#18); all four bugs are latent today.

- **cvar.py (short-sign):** correlation & sector stress clamped **short** losses
  to $0 (used the biggest *down* day for shorts, then negated it → a gain that
  `min(.,0)` zeroed). Shorts are now stressed by their biggest *up* day, longs by
  the biggest down day. Defaults made symmetric (±10% / ±15%).
- **cvar.py (dimension):** liquidity/catalyst gap losses divided ATR by market
  value, cancelling size → a ~constant loss per position. New `_gap_loss` =
  `N×ATR×shares` (size-proportional fallback when shares unknown; bounded at
  market value). Added `shares` to `PortfolioPosition`.
- **circuit_breakers.py (CB4):** the "2 trading days" halt added 2 *calendar*
  days, so a Friday trigger expired over the weekend (zero halt). New
  `_add_trading_days` (business-day offset); halt blocks through the day
  inclusive. Friday loss → blocks Mon+Tue, resumes Wed.
- **risk_engine.py (catalyst premium):** `size_position` used
  `catalyst_atr_premium` (1.5) as the whole multiplier, dropping the premium
  (catalyst got the same 1.5× stop as non-catalyst). Now `atr_stop_multiplier ×
  (premium if catalyst else 1) = 2.25`, matching `order_manager`. Test fixtures
  corrected to treat the config value as a premium factor.
- Deferred to #18: persisting CB state to a table (memory-only, but moot until
  CB is fed).
- Tests: +19.

**Verified:** full suite **445 passed, 0 failed**.

---

### 2026-06-24 — Cluster K (Tier 3): wire the real risk layer (#18)

**Files:** `config/config_loader.py`, `config/arconian_config.yaml`, `models.py`,
`risk/circuit_breakers.py`, `risk/regime_inputs.py` (new), `execution/order_manager.py`,
`main.py`, + tests (`test_regime_inputs.py`, `test_circuit_breakers.py` new;
`test_order_manager.py`).

Decisions (operator): **shadow-mode-first behind a config flag**, **regime via
yfinance ^VIX + IWM**, **sector_etf as the sector key now, enrich later**.

- **#18a** `risk.full_engine_mode` (off | shadow | enforce, default off) added to
  RiskConfig + YAML (quoted) + `_REQUIRED_KEYS`.
- **#18b** circuit-breaker state persisted to a new `circuit_breaker_state`
  singleton table; `CircuitBreakerManager.load`/`.persist` round-trip it, so
  streaks/drawdown/halts survive restarts (was memory-only — F-17).
- **#18c** `risk/regime_inputs.fetch_regime_inputs` pulls ^VIX + IWM and computes
  the VIX level + IWM 10d/20d returns `current_regime` needs (degrades to None).
- **#18d** `auto_paper_trade_candidates` honours the mode: shadow logs
  `approve_new_position` decisions without changing behavior; enforce skips
  rejects and opens approvals with the engine's regime/CB-adjusted sizing
  (`open_paper_trade` gained a `risk_spec` param). Sector key = `signal_log
  .sector_etf`; sub_industry / returns_60d left None (those constraints inactive
  until enrichment — documented follow-on).
- **#18e** `feed_circuit_breakers_after_close` feeds realised P&L into CB1
  (per close) and CB2/3/4 (daily) after the trade check and persists; `main.py`
  loads CB state at startup, computes regime per scan (shadow/enforce only), and
  passes `risk_engine`+`regime` into the auto-trade call.

Default is **off**, so live auto-trade behavior is unchanged until the operator
flips to shadow then enforce. Tests: +24.

**Verified:** full suite **463 passed, 0 failed**.

---

### 2026-06-24 — Cluster L (Tier 3): exits, calendar, clock (F-19, F-26, F-33)

**Files:** `execution/order_manager.py`, `execution/scan_scheduler.py`,
`timeutils.py` (new), `models.py`, `main.py`, + 7 source modules and 6 test
files (utcnow migration).

- **F-19 (entry-day bar):** the exit walk skipped the entry day entirely, so an
  entry-day stop blow-through went unseen until the next bar — optimistic, the
  opposite of the system's pessimistic convention. The entry bar is now included
  and evaluated pessimistically: only an adverse breach (stop) is honored;
  target/TP1/time-stop are ignored on the entry bar (EOD data can't place the
  day's extremes relative to the intraday fill). `days_held` reindexed so full-day
  triggers are unchanged.
- **F-26 (early close + overlap):** added `is_early_close` / `regular_market_close
  _time` (1pm half-days: day-after-Thanksgiving, Christmas Eve, …). Scan jobs and
  `_within_market_hours` now respect the early close, so the 15:30 scan/auto-trades
  don't fire against stale post-close prices. The scheduler runs on a single-worker
  executor (+ `max_instances=1`) so the 16:15 maintenance and 16:30 trade check
  can't overlap on the DB.
- **F-33 (clock):** `timeutils.utcnow()` returns naive UTC the non-deprecated way;
  replaced all `datetime.utcnow()` call sites (source + tests) and the 4 model
  column defaults. DeprecationWarnings: ~370 → 0.
- Tests: +19 (entry-day long/short stop + favorable-ignored; early-close calendar
  + scan-guard + executor + auto-trade guard). utcnow migration is behavior-
  preserving.

**Verified:** full suite **478 passed, 0 failed**.

---

### 2026-06-24 — Cluster M (Tier 3): risk-engine input enrichment (#18 follow-on)

**Files:** `risk/position_inputs.py` (new), `execution/order_manager.py`,
`tests/test_position_inputs.py` (new), `tests/test_order_manager.py`.

- The initial #18 wiring left `sub_industry`/`returns_60d` as None, so the
  sub-industry hard limit and the Ledoit-Wolf correlation-cluster limit never
  fired. `risk/position_inputs.fetch_position_risk_inputs` now pulls the yfinance
  'industry' (GICS sub-industry proxy) and a 60-day daily-return array via the
  existing adapter, degrading to None on failure (constraint stays inactive for
  that name, never crashes). Wired into the candidate and every open position
  (and the within-batch book) in `auto_paper_trade_candidates`.
- With this, **all** RiskEngine constraints are active in shadow/enforce. Adds
  bounded yfinance calls per scan (candidates + ≤max_concurrent positions), only
  when the engine runs. Tests: +10.

**Verified:** full suite **488 passed, 0 failed**.

---

### 2026-06-24 — Cluster N (Tier 3): end-to-end engine test + config-key bug

**Files:** `risk/risk_engine.py`, `tests/test_risk_engine.py`,
`tests/test_risk_integration.py` (new).

- Added an end-to-end test driving the **real** `RiskEngine` through
  `auto_paper_trade_candidates` with the **real** config — a path no test
  covered (order_manager used a stub engine; RiskEngine was unit-tested alone).
- It immediately caught a production bug: `size_position` read
  `self._cfg.risk.tp1_r_multiple`, but `tp1_r_multiple` lives in the **execution**
  config. `RiskConfig` has no such field, so shadow/enforce mode raised
  `AttributeError` the moment it sized a non-crisis position — masked only
  because the RiskEngine unit-test mock had set the key on `.risk`. Fixed to read
  `self._cfg.execution.tp1_r_multiple`; corrected the test fixture to match.
- Tests: +3 e2e (shadow opens; enforce+crisis blocks; enforce+normal opens with
  engine sizing).

**Verified:** full suite **491 passed, 0 failed**.

---

## Summary of this session

Tier-1 of the DIAGNOSIS fix plan is **complete**, plus several incidental
hardening items. Test suite went **361 passed / 2 failed → 390 passed / 0
failed** (+31 tests, all green). Findings addressed: F-1, F-2, F-3 (minimal),
F-4, F-5, F-6, F-7, F-8, F-9, F-11, F-12, F-15, F-27, F-29, F-31, F-32, plus all
6 operator decisions.

**Not yet done (deferred, see DIAGNOSIS §5):**
- Tier 2 remaining: Pass-2 chain volumes → directional confirmation (F-18) —
  **moot while `options_enabled=false`** (Pass-2 enrichment is gated off, so the
  volumes aren't fetched); revisit alongside the Phase-2 options store.
  *(Done across sessions: F-10 cost validator, F-24 outcome dead-letter,
  F-20 history cliff, F-14 universe isolation, F-13 Schwab 401, F-23 parameter
  baseline, F-25 IC segment/direction, F-16/F-35/F-36 latent data bugs.)*
- Tier 3: full RiskEngine wiring + its dead-code math bugs (F-17), entry-day bar
  evaluation (F-19), early-close calendar / job overlap (F-26),
  `datetime.utcnow()` migration (F-33), the Phase-2 options IV/PC history store
  (to re-enable `options_enabled`).

**Operational note:** the 2026-05-06→05-12 paper-trade cohort is still in the DB
and should be marked known-bad before the paper-trade clock restarts
(decision #3) — that's a data step, not a code change, and was not performed.
