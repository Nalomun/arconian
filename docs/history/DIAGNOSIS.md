# Arconian — Diagnostic Report (2026-06-10)

Scope: full read of the critical path (`main.py`, `signals/signal_engine.py`, `execution/order_manager.py`,
`db.py`, `models.py`, `config/`, `data/yfinance_adapter.py`, `data/utils.py`, all five signal modules,
`journal/outcome_collector.py` key paths) plus delegated deep reviews of `universe/`, `risk/`, `journal/`,
`execution/scan_scheduler.py` + `deadman_switch.py`, and the remaining `data/` adapters. Evidence was
cross-checked against the live database (`arconian.db`), `logs/arconian.log*`, and the test suite
(361 passed / 2 failed — see F-31). No code was modified.

---

## 1. Critical path and current operational state

```
APScheduler (ET cron, market-day gated)
  ├─ 09:35 / 12:00 / 15:30  _do_scan(scan_type)
  │     SignalEngine.run_scan
  │       ├─ load ACTIVE+OBSERVATION tickers (universe_state)
  │       ├─ yf.download 6mo OHLCV (323 tickers, batched) + sector ETF closes (3mo, 24h cache)
  │       ├─ Pass 1 per ticker: volume pctiles, return pctile, sector RS, delay (from DB), ATR, earnings/corp-action exclusion
  │       ├─ Pass 2: Schwab option chains for top 40 → options composite
  │       ├─ directional confirmation, retail-attention penalty
  │       └─ write ALL rows to signal_log
  │     auto_paper_trade_candidates (score ≥ 0.70, direction != ambiguous, …)
  │       └─ open_paper_trade → trades row + signal_log.was_traded/trade_id
  ├─ 08:50  universe daily check (state machine, delay-score recompute)
  ├─ 16:15  maintenance → outcome_collector / cost_validator / ic_tracker
  ├─ 16:30  check_open_trades → walk EOD bars: stop → TP1 → trailing → time stop → P&L → signal_log
  └─ DeadmanSwitch watchdog (60-min heartbeat timeout → widen stops → force-close → halt)
```

**Current state (verified against DB and logs):**
- The daemon is **not running**. Last clean start/stop was 2026-06-09 15:46–15:47 on this Linux machine.
  No scans have produced data since **2026-05-12**.
- 107 paper trades exist (2026-05-06 → 05-12). **101 of 107 were force-closed by the deadman switch**,
  all with `exit_price = NULL` and `realized_pnl = NULL`. Only 6 trades have real outcomes (all stops).
  The Phase-1 evaluation dataset is effectively destroyed.
- Same-ticker pyramiding is real: e.g. MNKD ×5 trades opened 2026-05-06; many tickers ×3 (one per scan).
- `ic_history` shows `options` IC = NULL on 87 observations (zero variance — see F-4) and negative
  composite IC (−0.109) on the small contaminated sample.
- 3,747 of 4,247 signal_log rows have `return_3d = NULL` (the labeling pipeline barely ran before the
  system stopped).
- CONTEXT.md describes a Windows host; the repo now runs on Linux (Fedora, Python 3.13,
  pandas 3.0.2, yfinance 1.3.0). At least one finding (F-6) is environment-regression caused by this move.

---

## 2. Findings

Severity: **Blocker** = live flow broken or producing wrong output now · **High** = wrong/silent data or
fragile single point of failure · **Medium** · **Low** · **Nit**.
Confidence: **CONFIRMED** = code path read end-to-end (and where noted, verified against DB/logs/empirics) ·
**SUSPECTED** = looks wrong, not fully verified.

### Blockers

| ID | Location | Conf. | What's wrong | Why it matters |
|----|----------|-------|--------------|----------------|
| F-1 | `execution/deadman_switch.py:60-89,140-203` + `scan_scheduler.py:186-192` | CONFIRMED (logs + DB) | The 60-min deadman timeout is structurally shorter than every legitimate heartbeat gap (scans are 131–210 min apart; overnight ~18 h). Only scans heartbeat; the 16:15/16:30 jobs `ensure_armed()` but never feed it. The closed-market "keep-alive" heartbeat itself arms the timer. `heartbeat()` also clears `_halted`, so a halt silently un-halts next scan. | Fired repeatedly in production (2026-05-06/07/11/12): widened stops on up to 49 trades, **force-closed 76, 16, 5, 2 trades** on healthy days. This single component destroyed the paper-trading dataset. |
| F-2 | `execution/deadman_switch.py:214-242` | CONFIRMED | Stage 2 unconditionally widens every open trade's `stop_price` by 1 ATR and commits — no gate, no flag. This directly violates hard design constraint #4 ("Never widen stops", CONTEXT.md), though `arconian-v3.md` §deadman does describe the stage (the two docs conflict). | Stops were widened on live paper trades multiple times in May. Also: `_force_close_all()` (lines 244-267) writes `status='closed'` with **no exit_price / realized_pnl** → 101 trades with NULL P&L (verified in DB). |
| F-3 | `main.py:198-202` + `execution/order_manager.py:681-798` | CONFIRMED (code + DB) | The entire risk layer is constructed and then never used. `auto_paper_trade_candidates` applies **no** max-concurrent-positions cap, no per-ticker dedup, no circuit breaker, no regime check, no sector/correlation constraint. `RiskEngine`, `CircuitBreakerManager`, `PortfolioConstraintChecker`, and `cvar.py` have zero production call sites; breaker state is also never fed (`record_trade`/`record_day` uncalled). | Verified consequence: 50+ positions opened per day, same ticker opened up to 5×, each at 0.5% risk. Every documented §4.3–4.5 safety is inert while auto-trading is live. |
| F-4 | `signals/signal_engine.py:509-521` + `signals/options_signal.py:179-195` | CONFIRMED (code + DB) | `compute_options_signal` is invoked without IV history, 52-w IV range, P/C history, or concentration inputs, so every sub-component takes its 0.5 neutral default: the options composite is **a constant 0.5** for every ticker that passes the liquidity floors. Verified: IC for `options` is NULL (zero variance) over 87 observations. | 15% of the composite weight is decorative — and worse than absent: weight renormalisation pulls high scorers that *pass* the floors down toward 0.5 while suppressed names keep their full score, perversely reshuffling who crosses the 0.70 auto-trade threshold. The "critical missing piece" per the backtest analysis is silently still missing in live data. |
| F-5 | `main.py:204-209` + `signals/signal_engine.py:721-723` | CONFIRMED | `EarningsCalendar` is never instantiated anywhere; `SignalEngine` is built without it, and `_check_earnings` then tags every ticker `"normal"`. Same pattern: `edgar_adapter` is passed in but `self._edgar` is never used (engine line 233), and `data/macro_calendar.py` has no importers. | Earnings exclusion, EDGAR corporate-action detection, and macro blackouts — all described as live exclusion layers — have **never fired in production**. Tickers reporting earnings tomorrow are scored and auto-traded. The `earnings_proximity_tag != 'excluded'` filter in auto-trade is vacuous. |
| F-6 | `journal/outcome_collector.py:166` | CONFIRMED | `_triple_barrier_label(..., stop_price=event.entry_price)` passes the trade's **fill price** as the stop barrier. For longs, any later day whose low touches the entry price labels the row −1. | Nearly every traded signal will be labeled −1 regardless of actual outcome. Poisons exactly the rows ML training cares most about. (Currently latent only because the labeling pipeline barely ran.) |

### High

| ID | Location | Conf. | What's wrong | Why it matters |
|----|----------|-------|--------------|----------------|
| F-7 | `execution/order_manager.py:669` (`_fetch_entry_price` fallback) | CONFIRMED (empirically reproduced) | `float(df["Close"].iloc[-1])` raises `TypeError` on this environment: yfinance 1.3.0 returns MultiIndex columns for single-ticker downloads and pandas 3.0 removed `float(Series)`. The exception is swallowed → returns None → candidate skipped (`price_fetch_failed`). | The off-hours entry-price fallback — added 2026-04-30 specifically to fix "candidates=50 opened=0" — is re-broken by the Windows→Linux/pandas-3 migration. Any scan that fires outside market hours (misfire catch-up after sleep) opens zero trades again. |
| F-8 | `signals/signal_engine.py:484-486` | CONFIRMED | Sector-RS spread history misalignment: `closes` covers 6 months, `etf_closes` 3 months; `closes[:min_len]` takes the **oldest** ~63 stock days (3–6 months ago) and pairs them with the ETF's most recent 63 days. The spread history the percentile rank is computed against is meaningless. | A 10%-weight signal silently computed from garbage every scan. Should be tail-aligned (`[-min_len:]`), ideally date-aligned. |
| F-9 | `signals/signal_engine.py:288-299, 442-476` | CONFIRMED (path) / SUSPECTED (trigger frequency) | No per-ticker exception isolation in the Pass-1 loop, while `closes`, `volumes`, `highs`, `lows` are independently `dropna()`'d (`_extract_ohlcv:819-822`) and then multiplied/indexed against each other (`closes * volumes`, `_compute_atr` indexes `h[i]` by `len(c)`). Unequal lengths raise (or worse, equal-but-shifted lengths silently mis-multiply). | One ticker with a NaN-Close/non-NaN-Volume row kills the **entire scan** (no signal_log rows, no trades, no heartbeat → can also trip the deadman). Misaligned-but-equal lengths corrupt dollar-volume and ATR silently. |
| F-10 | `journal/cost_validator.py:162-171` | CONFIRMED | `slippage_bps = abs(round-trip P&L in bps)`; `adverse_selection_bps = max(0, |P&L| − 30)`. These are not cost measurements at all. The rolling exceedance check then compares mean \|P&L\| (hundreds of bps for 3% movers) to a ~36–50 bps working estimate. | Execution-quality columns in signal_log are garbage for cost-model calibration, and the exceedance flag will be a permanent false alarm once trades exist. |
| F-11 | `journal/outcome_collector.py:128-168` + `journal/signal_log.py:46` | CONFIRMED | Eligibility is 3 **calendar** days but the window is trading days, and labeling proceeds with as few as 2 bars: Thu/Fri signals get a premature time-barrier `outcome_label=0` and MAE/MFE written days early (later corrected only if data completes). MAE/MFE also include day-0's full intraday range while the entry proxy is the day-0 **close** — systematically inflating MFE on spike events. The barrier code correctly excludes day 0; MAE/MFE don't. | Directionally biased labels and excursions feeding future ML; transiently (or for stranded rows, permanently) wrong labels every week. |
| F-12 | `execution/scan_scheduler.py:80-94,132,184` | CONFIRMED | `good_friday()` computes `date(year, easter.month, easter.day − 2)` with no month rollover. Easter 2029 falls on Apr 1 → `date(2029, 4, -1)` raises `ValueError`; `is_market_open()` is called outside the job try/except. | Every scheduled job in calendar year 2029 aborts. A deterministic time bomb. |
| F-13 | `data/schwab_adapter.py:383-403` | CONFIRMED (path) | When the 7-day refresh token dies mid-session, every call 401s, is retried 3× with 10 s sleeps, and returns None — indistinguishable from a transient blip. `get_account_info` (the "token heartbeat") has no callers. | A post-expiry scan burns ~13+ min in sleeps across 40 chain calls, completes "successfully" with all-None Schwab data, and never alerts that reauth is needed. Token fragility is a known, recurring operational failure (CONTEXT.md). |
| F-14 | `universe/universe_manager.py:245-262` | CONFIRMED | The whole daily check — including per-ticker network calls — runs in one `session_scope()` transaction with no per-ticker isolation. Also `universe_state.py:71-89`: an all-None day (total data outage) counts as a **pass**, resetting fail counters and advancing reactivation hysteresis. | One bad ticker rolls back the whole day's state machine; the multi-minute write transaction locks SQLite against the live scheduler; outage days corrupt suspension hysteresis and can reactivate suspended tickers. |
| F-15 | `data/earnings_calendar.py:113,128` | CONFIRMED (latent — module unwired, see F-5) | Exclusion windows are **calendar** days; config documents trading days. With ±1 day: Friday AMC reporters are not excluded on Monday (gap = 3), Monday reporters not excluded on Friday. | When F-5 is fixed, the exclusion will still systematically miss the weekend cases — the most common earnings timing. Fix together. |
| F-16 | `data/edgar_adapter.py:47,51` + `data/macro_calendar.py:28,83-96` | CONFIRMED (latent — modules unwired) | EDGAR item map is wrong (Item 1.01 "Material Definitive Agreement" mapped to merger/999-day exclusion; Item 8.01 "Other Events" mapped to ticker_change). Macro calendar lists June 2026 FOMC as Jun 9–10; the Fed's published calendar says **Jun 16–17** (other months verified correct); table is 2026-only and silently returns None from 2027. | If wired as-is, routine 8-Ks would exclude tickers near-permanently, and the system would blackout the wrong FOMC week this month. |
| F-17 | `risk/cvar.py:137-141,166-176,209-233` + `risk/circuit_breakers.py:26-28,180` (latent — module dead, F-3) | CONFIRMED | Shorts contribute $0 to correlation/sector stress (sign error clamps short losses to zero); `_liquidity_shock_loss` is dimensionally wrong (loss ≈ constant per position regardless of size); CB state is memory-only (every restart zeroes streaks/drawdown); CB4's "2 trading days" halt is 2 calendar days (Friday trigger → zero effective halt). | These must be fixed before the risk layer is ever wired in (which F-3 requires). Listed High rather than Blocker only because the code is currently dead. |

### Medium

| ID | Location | Conf. | What's wrong | Why it matters |
|----|----------|-------|--------------|----------------|
| F-18 | `signals/directional_confirmation.py:69-99` (as used by engine) | CONFIRMED | In live Phase 1, VWAP is always None and options vols are never passed (`signal_engine.py:543-550` hardcodes `call_vol=None, put_vol=None` even after Pass-2 enrichment), so direction reduces to the sign of `return_1d`. The whitepaper expects 30–50% ambiguous; actual is ~0%. | The "confirmation layer" gate on auto-trading is vacuous — every nonzero-return candidate gets a direction and trades. Also: Pass-2 fetches chain volumes but never feeds them to directional confirmation. |
| F-19 | `execution/order_manager.py:316-320` | CONFIRMED | `check_days` filter `entry_date < d <= as_of_date` skips the entry day entirely: a stop blown through on entry day isn't seen until the next day's bar, and TP1's same-bar trailing-stop breach is skipped (`continue`). | Optimistic bias in paper-trade exits — the opposite of the stated "pessimistic on ambiguous bars" convention. Inflates paper expectancy. |
| F-20 | `data/yfinance_adapter.py:143-176` + `signals/volume_signal.py` | CONFIRMED | `period="6mo"` ≈ 124–126 trading days; the 120-day volume window needs 121. Any ticker with a few missing rows drops below 120 → `short_history=True` → `history_status='short'` → silently **ineligible for auto-trading** (filter in `auto_paper_trade_candidates`). | A data hiccup flips trade eligibility with no log trail. Margin is ~4 days. DB shows it's currently rare (24 'short' rows) but it's a cliff, not a slope. |
| F-21 | `universe/universe_manager.py:619-640` + `data/schwab_adapter.py:236-245` | CONFIRMED | Spread filter uses a single pre-market quote snapshot (vs documented 20-day average); bid=0/ask=0 yields spread=0 → vacuous pass. Midday dollar volume is fabricated as 0.30 × daily volume and stored as if measured. Option-chain "None" (no options listed) is treated as inconclusive → pass, so the options-OI/strike filters can't exclude option-less tickers. | Universe membership — the top of the funnel — is partly governed by synthetic or vacuous checks. |
| F-22 | `universe/delay_score.py:86,116-122,150-154,215` | CONFIRMED | Weekly lag construction is positional across gaps (lag-1 may be 2+ weeks back); `inf` returns from zero closes survive and produce a confident **0.0** delay score written to the DB (not retried for 25 days); the min-days-per-week guard counts NaN rows. | Delay is a 10%-weight signal and the universe eligibility criterion; silently wrong precisely for the illiquid names the thesis targets. |
| F-23 | `journal/parameter_tracker.py:36-66` | CONFIRMED | The "last snapshot" is the most recent `triggered_by='startup'` row, but after any real change that row holds a single scalar, so the next startup diffs the full config against a scalar and writes a garbage row with `parameter_path=""`; timestamp ties make row selection arbitrary. | The parameter audit trail (feature-versioning support) self-corrupts after the first config change. |
| F-24 | `journal/outcome_collector.py:94,122` + `journal/signal_log.py:41-50` | CONFIRMED / SUSPECTED (day-0 shift) | Fixed `fetch_end = signal_date + 7` forever: rows that can't get 4 bars in that window (delisting, halt) stay `return_3d=NULL` and — because `get_unresolved_events` is `ASC LIMIT 500` — permanently clog the head of the queue, eventually blocking all newer labeling. If the signal date has no bar, the next day silently becomes day 0. | Slow-motion pipeline deadlock plus silent window shift. |
| F-25 | `journal/signal_log.py:177-178` + `journal/ic_tracker.py` | CONFIRMED | The IC segment named `pre_earnings` actually selects `post_earnings_drift` rows. Also `return_3d` is raw long-convention while `outcome_label` is direction-adjusted; IC pools bearish rows against raw returns, attenuating measured IC. | ic_history segment data is mislabeled; IC — the system's go/no-go metric — is biased toward zero if bearish signals are material. |
| F-26 | `execution/scan_scheduler.py` (whole) + `deadman_switch.py:157-203` | CONFIRMED | Early-close days (Black Friday, Christmas Eve) run the full schedule against a closed market; jobs in different APScheduler threads can overlap (maintenance at 16:15 still running at 16:30 trade check); deadman escalation can't be aborted by `halt()` mid-sleep, and the heartbeat-recency check has a TOCTOU window before `_widen_stops`. | Stale-quote scans and racey afternoons; deadman remains dangerous even after F-1's cadence fix unless these are addressed. |
| F-27 | `config/config_loader.py:161-263` | CONFIRMED | `execution.auto_paper_trade`, `auto_paper_trade_min_score`, `account_equity` are not in `_REQUIRED_KEYS` — no startup validation. If deleted, silent dataclass defaults take over (equity 10,000 vs configured 26,000 → 2.6× sizing change). | The three parameters that directly control live auto-trading are the unvalidated ones. |
| F-28 | `data/yfinance_adapter.py:284` + `get_sector_etf_prices` | CONFIRMED (path) / SUSPECTED (impact) | Cached path returns `pd.read_json(string)` — deprecated calling convention (pandas 3) and epoch-int round-trip of the index; fresh vs cached frames may differ in index dtype. `_extract_etf_prices` only uses columns, so impact today is low, but the 24 h cache means F-8's "3 months of ETF closes" can also be a day stale at the open scan. | Fragile serialization on the live path; verify behavior on pandas 3 when fixing F-8. |

### Low / Nit (abbreviated)

| ID | Location | Conf. | What's wrong |
|----|----------|-------|--------------|
| F-29 | `signals/signal_engine.py:435-439` | CONFIRMED | `si_pct` assigned and never used in Pass 1 (dead code; SI is re-fetched in `_apply_retail_attention`). |
| F-30 | `universe/universe_manager.py:536-541` | CONFIRMED | `suspension_reason` overwritten by any later one-day blip of a different filter — audit trail loses the real suspension cause. |
| F-31 | `tests/test_scan_scheduler.py:243-246,358-369` | CONFIRMED | The "2 flaky deadman tests" are not flaky — they fail deterministically; they encode the pre-refactor arm-at-init contract. The suite is otherwise green (361 passed). |
| F-32 | `.cache/` tracked in git | CONFIRMED | Hundreds of mutable cache JSONs are committed and show as perpetually modified; should be gitignored. |
| F-33 | repo-wide | CONFIRMED | `datetime.utcnow()` used throughout — deprecated in 3.12+, warnings in test output; internally consistent today. |
| F-34 | `journal/ic_tracker.py:270` / `outcome_collector` | CONFIRMED | `computed_at` repurposed to hold period_start; `return_from_signal_time_3d` never computed (stays NULL); decay re-run double-counts today's row. |
| F-35 | `data/social_adapter.py:118,148-153,234` | CONFIRMED (latent — unwired) | Failed StockTwits fetch caches 0 mentions for 1 h and reports `stocktwits_available=True`; endpoint likely 403s for scripted clients. Fail-open exactly on meme names. |
| F-36 | `data/schwab_adapter.py:385-387,449,453-457` | CONFIRMED (latent) | Chain aggregates all expirations incl. LEAPS (inflates OI vs floors); Schwab IV is in percent while `compute_options_signal` documents decimal (100× unit bug armed for whoever wires IV rank); `distinct_lot_sizes` is per-strike volume rounded to 10s, not trade sizes. |

---

## 3. Verified OK (positive coverage)

- **Composite math** (`compute_composite`): NULL propagation, weight renormalisation, [0,1] clipping, penalty application — correct. Stored effective weights match the renormalised values used.
- **Volume / return signal math**: window selection, percentile ranks, short-history flagging — correct. ATR formula correct (given aligned inputs; see F-9).
- **Sizing** (`_compute_sizing`): risk-per-trade ÷ stop distance, catalyst premium 1.5×1.5=2.25 (matches spec), max-position cap, zero-share guards — correct. (RiskEngine's duplicate implementation disagrees — see agent finding under F-17 family — but it's dead code.)
- **Trade exit walk** (`_check_one_trade`): pre-TP1 stop-beats-target pessimism, TP1 partial accounting, trailing ratchet direction, time-stop next-open convention, P&L and R-multiple math for both legs and both directions — correct (modulo F-19's entry-day skip).
- **auto_paper_trade filters**: SQL semantics checked — NULL composite/tag rows are correctly excluded; `since` timestamp (UTC) is consistent with `scan_timestamp`.
- **db.py**: WAL/busy_timeout/foreign-keys pragmas per connection; `session_scope` commit/rollback/close correct; `expire_on_commit=False` consistent with the detached-object usage in order_manager; idempotent column migrations fine.
- **Scheduler timezone handling**: `CronTrigger(timezone=US/Eastern)` with correct pytz usage; DST safe; misfire grace + coalesce present on all 6 jobs; standard NYSE holidays (except F-12's 2029 bug and early-closes) correctly skipped; a failed scan correctly withholds the heartbeat.
- **maintenance.py**: per-job exception isolation real; summary keys match `main.py` consumption exactly.
- **Triple-barrier conventions** (given correct inputs): stop-before-target tie-break, ±1/0 orientation for long and short, idempotent OutcomePrice writes, `is not None` guards (no falsy-zero bug).
- **IC mechanics**: SQL-level NULL filtering, pairwise component-score dropping, ≥5-pair minimum, NaN→None; unique-constraint upsert path doesn't duplicate.
- **UTC date fix in universe/**: complete — no remaining `date.today()`/local-time mixes in universe code (the fix described in CONTEXT.md held).
- **Schwab adapter hygiene**: 110/min limiter under cap, ≤100-symbol batches, Retry-After honored on quotes, −999 IV sentinel filtered, no connection leaks.
- **Config loader**: attribute surface matches every production access site; missing/mistyped required keys fail loudly at startup with aggregated errors (except F-27's gaps).
- **delay_score regression math**: lag alignment, restricted-⊂-full R² bound, guard rails — correct for gap-free weekly series (gaps are F-22).
- **`partial_fill_manager.py`**: inert stub, not instantiated. `_assert_paper_trading_disabled` correctly blocks live orders while `paper_trading: true`.

---

## 4. Open questions (intent unclear — need your call)

1. **Deadman scope in Phase 1.** `arconian-v3.md` specifies widen-stops/force-close stages; CONTEXT.md's hard constraint #4 says never widen stops. For paper trading, should the deadman be alert-only (no DB mutation)? My recommendation: yes — Stages 2/3 only make sense guarding live capital, and even then force-close-via-DB-update is meaningless without a broker order.
2. **Was bypassing the risk engine in Phase-1 auto-trading intentional** (collect maximum signal data) or an omission? Even if intentional, is unlimited same-ticker pyramiding (MNKD ×5) wanted? `max_concurrent_positions: 5` exists in config and is currently meaningless.
3. **Phase-1 dataset disposition.** 101/107 trades have NULL P&L from spurious force-closes; signal rows from the contaminated cohort feed IC. Restart the paper-trade clock after fixes, and/or mark the 2026-05-06→05-12 cohort known-bad alongside the April cohort?
4. **Retail penalty weight** 0.20 (config) vs 0.10 (whitepaper) — still unresolved from CONTEXT.md.
5. **Off-hours auto-trades** (F-7): when a scan misfires and runs after close, do you *want* paper trades opened at stale EOD prices, or should off-hours candidates be skipped by design? The current fallback implies "yes, open them" — confirm before fixing it.
6. **Options sub-component data**: IV rank and P/C-ratio history require a per-ticker history store that doesn't exist yet. Build it (new small table or cache), or explicitly hold options at neutral and remove its weight until Phase 2? Constant-0.5 (status quo) is the worst of both worlds (F-4).

---

## 5. Fix plan — NOT executed; awaiting approval

Ordered by severity, then blast radius. "Safe" = behavior-preserving or unambiguously-correct fix that
respects existing architecture; "Decision" = needs an answer from §4 first.

### Tier 1 — stop the bleeding (do before the daemon runs again)

| # | Fix | Files | Blast radius | Safe? | Verify by |
|---|-----|-------|--------------|-------|-----------|
| 1 | **Defang the deadman for Phase 1**: make Stages 2/3 alert-only (Telegram CRITICAL, no DB writes), keyed off a config flag defaulting to alert-only; make `halt()` abortable mid-escalation and halts sticky (heartbeat does not clear `_halted`). Fix arming: only scans arm/feed it, and set the timeout from the *scheduled gap to the next scan* + grace (or simplest: timeout = 18h). | `execution/deadman_switch.py`, `execution/scan_scheduler.py`, `config/arconian_config.yaml` | deadman only | **Decision (Q1)** on stage semantics; the sticky-halt and arming fixes are Safe | Unit tests for arm/feed cadence; soak the daemon over a weekend and confirm zero spurious escalations in the log |
| 2 | **Gate auto-trading with the existing constraints**: in `auto_paper_trade_candidates`, skip tickers with an already-open trade, and stop opening once open-trade count ≥ `config.risk.max_concurrent_positions`. (Full RiskEngine wiring is Tier 3 — this is the minimal cap using existing config, no new abstractions.) | `execution/order_manager.py` | auto-trade path only | **Decision (Q2)**, but minimal version is low-risk | Regression test: 50 candidates, cap 5, same ticker twice → 5 opened, dedup respected |
| 3 | **Wire `EarningsCalendar` into `main.py`** (construct it, pass to `SignalEngine`) and convert its windows to trading days (F-15) in the same change. | `main.py`, `data/earnings_calendar.py` | scan path; adds yfinance earnings calls (6 h cached) | Safe in direction (only adds exclusions) | signal_log rows near known earnings get `earnings_proximity_tag='excluded'`; spot-check a Friday-AMC reporter |
| 4 | **Fix entry-price fallback for pandas 3** (F-7): flatten/`xs` MultiIndex in `_fetch_entry_price` (reuse the `_extract_ohlc_open` handling) and use `.item()`/scalar access. | `execution/order_manager.py` | one helper | Safe — **after Decision (Q5)** confirms the fallback should exist at all | The empirical repro in this report flips from TypeError to a float; existing 6 regression tests still pass |
| 5 | **Fix sector-RS alignment** (F-8): align stock and ETF closes on dates (or at minimum tail-align `[-min_len:]`). | `signals/signal_engine.py` | sector RS values change (they were wrong) | Safe (correctness fix; output values will shift) | Hand-compute one ticker's spread history vs the code's output |
| 6 | **Per-ticker exception isolation + array alignment** (F-9): wrap the Pass-1 per-ticker call in try/except (log, continue), and extract OHLCV by joint-dropna over the aligned frame instead of four independent `dropna()`s. | `signals/signal_engine.py` | scan robustness | Safe | Inject one corrupt ticker in a unit test; scan completes for the rest |
| 7 | **Don't label with the entry price as a stop** (F-6): for traded rows, pass the trade's actual `stop_price` (it's on the Trade row); for untraded, keep ATR-proxy barriers. Also gate labeling on having all 4 bars (fixes the premature-label half of F-11) and exclude day 0 from MAE/MFE to match the barrier convention. | `journal/outcome_collector.py` | labeling only; affects future + reprocessed rows | Safe | Unit tests: long trade dipping to entry but closing +5% labels +1, not −1; Thu signal not labeled until 3 trading days exist |
| 8 | **Fix `good_friday` rollover** (F-12): compute via `easter − timedelta(days=2)`. One line. | `execution/scan_scheduler.py` | none | Safe | `good_friday(2029) == date(2029, 3, 30)` test |

### Tier 2 — data correctness (before trusting any new Phase-1 numbers)

| # | Fix | Files | Blast radius | Safe? | Verify by |
|---|-----|-------|--------------|-------|-----------|
| 9 | **Options signal: pick a lane** (Q6). Either (a) build the minimal per-ticker IV/PC history store (e.g. a small `options_history` table appended each Pass 2 — it self-populates over ~60 days) or (b) set options weight to 0 in config until the store exists, letting renormalisation do its documented job. Do **not** leave the constant-0.5. | `signals/`, `models.py` or config | composite scores shift | **Decision (Q6)** | IC(options) becomes non-NULL (a) or weight_options disappears from new rows (b) |
| 10 | Feed Pass-2 chain volumes into directional confirmation (the data is already fetched and discarded) (F-18). | `signals/signal_engine.py` | direction labels | Safe | Ambiguous rate rises from ~0%; spot-check a high-P/C bearish name |
| 11 | Cost validator: record measured-vs-intended fill costs only when both sides exist; stop writing \|P&L\| into slippage; make components sum (F-10). | `journal/cost_validator.py` | execution-quality columns | Safe (current values are garbage) | Unit test with a known fill pair |
| 12 | Outcome queue: add a max-age dead-letter (e.g. mark `outcome_label` unresolvable after N days) so the ASC LIMIT 500 queue can't clog (F-24); detect missing day-0 bar and skip with a warning. | `journal/outcome_collector.py`, `journal/signal_log.py` | labeling pipeline | Safe | Backfill run on the existing 3,747 NULL rows drains instead of stalling |
| 13 | Universe daily check: per-ticker try/except, short write-transactions (fetch outside the session), treat all-None as *inconclusive* (no counter changes) rather than pass (F-14). | `universe/universe_manager.py`, `universe/universe_state.py` | universe state machine | Safe | Simulated outage day leaves counters untouched |
| 14 | Schwab 401 discrimination (F-13): catch auth errors distinctly, alert via Telegram once, and short-circuit remaining Schwab calls for that scan. | `data/schwab_adapter.py` | Schwab paths | Safe | Revoke-token test: scan completes fast with one CRITICAL alert |
| 15 | `pre_earnings` segment filter, IC direction convention (decide: sign-adjust returns by direction, or document raw-only), parameter_tracker full-snapshot baseline (write a real full-config snapshot row each startup) (F-23, F-25). | `journal/` | analytics only | Mostly Safe; IC convention is a small **Decision** | ic_history rows match their segment names; second startup after a config change writes a clean diff |
| 16 | Fetch 12mo instead of 6mo in `get_daily_prices` (or compute `history_status` from `universe_state.history_days`) to kill the 120-day cliff (F-20). | `signals/signal_engine.py` or `data/yfinance_adapter.py` | scan data volume (~2× download) | Safe | Tickers with full history but one missing row stay `full` |
| 17 | Fix latent-but-armed data bugs so they can't bite when wired: EDGAR item map, macro June FOMC date + 2027 None-warning, Schwab chain date-bounds + IV units note, social-adapter fail-open caching (F-16, F-35, F-36). | `data/` | none today | Safe | Unit tests against known filings/calendar |

### Tier 3 — structural (needs decisions; do after Tier 1+2 are soaking)

| # | Fix | Notes |
|---|-----|-------|
| 18 | Wire the real risk layer (Q2): route `auto_paper_trade_candidates` through `RiskEngine.approve_new_position`, feed `cb_manager.record_trade/record_day` from trade closes and daily equity, persist CB state to a small table. Fix the dead-code-only math bugs first (F-17: short-sign stress, liquidity-shock dimension, CB4 trading days, catalyst premium interpretation in RiskEngine). | Largest change; blast radius = every auto-trade. Gate behind config flag; verify in paper mode by comparing approved/rejected counts against hand-checks. |
| 19 | Decide F-19 (entry-day bar evaluation) — checking the entry day's bar pessimistically requires intraday knowledge you don't have from EOD data; an honest middle ground is to evaluate entry-day stop/target using the post-entry-time assumption you're willing to document. | Affects paper expectancy comparability. |
| 20 | Early-close calendar + job overlap serialization (F-26); update the two stale deadman tests to the lazy-arm contract (F-31); gitignore `.cache/` (F-32); migrate `datetime.utcnow()` (F-33). | Housekeeping batch. |

**Explicitly not proposed:** any try/except that swallows the misalignment in F-9 without logging, broad
refactors of the signal engine, new dependencies, or new abstractions. Every Tier-1/2 fix is a local change
to an existing module.

---

*End of diagnosis. No code has been modified. Awaiting your go-ahead and answers to §4 before any fix.*
