# Arconian — Forensic Project History

Reconstructed 2026-09-20 from the repository as it exists on disk. Sources, in order of trust:
the git object database (34 commits), file modification times, `arconian.db` (read-only queries),
`logs/arconian.log` and `logs/arconian_stdout.log*`, the test suite (executed today), and the
Python source. The whitepaper (`arconian-v3.md`), `arconian-supplementary-docs.md`, `README.md`,
`CONTEXT.md`, `DIAGNOSIS.md`, `FIXLOG.md` and the five `claude-code-prompt-*.md` task briefs were
read as claims and checked against those sources.

Conventions used below:

- **VERIFIED** = confirmed against git, DB, logs, a script re-run, or a code path read end to end.
- **DOC-ONLY** = stated in a document; no artifact in the repo confirms it.
- **NOT SHOWN** = the repo contains no evidence either way.
- Timestamps in logs are local machine time (ET on Windows, ET on the Fedora machine); DB
  timestamps are naive UTC.

> **Public snapshot note (2026-09-30).** Commit hashes and "git history" in this document refer to the private development repository; this public repository is a single-commit snapshot of it. Files cited by name have moved: the whitepaper and supplements to `docs/design/`, the Claude Code briefs to `docs/briefs/`, `DIAGNOSIS.md` and `FIXLOG.md` to `docs/history/`. `CONTEXT.md`, the backtest and audit CSVs (licensed Norgate data), and the `.cache/` files are not included. Corrections made after the original date are marked *[corrected]*.

---

## 1. Chronology from git

### 1.1 What git can and cannot tell you

Git history begins on **2026-05-05 21:21 ET** with a single commit, `1acd1a1 "bleh"`, that snapshotted
2,375 files (2,272 of them runtime cache JSONs under `.cache/`, plus the 137 MB backtest CSV and
the 1.15 MB extreme-return audit CSV). The remaining 33 commits are all from a single session on
**2026-06-24 between 01:36 and 04:06 ET**. Nothing has been pushed to a remote *[corrected: an `origin` remote was configured at `git init` but never pushed to]*. Nothing has been committed since.

Everything before 2026-05-05 (the whitepaper, the entire Phase 0 build, the backtest, the four
analysis reports, the paper-trader implementation, and the 2026-04-30 fix session) is **not in git
history**. The pre-git timeline below is reconstructed from file mtimes (which survived the
Windows-to-Linux move), daemon start lines in the logs, DB row timestamps, and the dated docs.

### 1.2 Timeline

| Date (2026) | Evidence | What happened |
|---|---|---|
| Feb 16 – Mar 31 | `../Zinniinae` file mtimes | Companion Streamlit journal Zinniinae predates Arconian; it has its own `backtester.py` (Feb 16). Out of scope here, but its `pages/09_arconian_live.py` reads `arconian.db` by a hard-coded Windows path, so it is broken on the current machine. VERIFIED. |
| Apr 6, 03:53 – 04:55 | mtimes | `arconian-v3.md` (99 KB whitepaper), `arconian-supplementary-docs.md`, `README.md` written. Package skeleton created 04:56. |
| Apr 6, 05:07 – 13:52 | mtimes | Alembic initial schema, `norgate_adapter.py`, test fixtures, `delay_score.py`, the five signal modules, `portfolio_constraints.py`. Module docstrings and file dates are consistent with the whole Phase 0 codebase being generated in one day from the whitepaper plus the "Recommended Module Structure" in the supplements. |
| Apr 6, 19:14 | `arconian.log` line 1 | First daemon start: "Arconian starting (Phase 0 — signal scoring only)". Windows (backslash config path). `parameter_history` baseline row written 22:14 ET. |
| Apr 9 – 10 | logs, `claude-code-prompt.md` (Apr 10 01:40) | Ten daemon restarts; scans return 0 candidates because `universe_state` is empty. Brief claims "316 tests pass". `scripts/seed_universe.py` written Apr 10 22:12. First `signal_log` rows Apr 10 (14 rows) and Apr 13 (27 rows). |
| Apr 11 | `overrides` table | Three non-equity tickers (BH-A, CRF, HYT) manually blocked, twice (duplicate rows). `universe_state` rows have `state_since` Apr 10–13: 326 tickers, 322 ACTIVE. |
| Apr 13 – 14 | logs | Deadman switch fires on healthy days (60-minute timeout; 0 open trades so no damage). First visible instance of the F-1 design flaw, two months before it was diagnosed. |
| Apr 16 | logs, `sector_rs_signal.py`, `reauth.py` mtimes | First full-universe scans: 323 tickers scored, twice (646 rows). Schwab reauth script added. |
| Apr 20 – 21 | `backtest.py` mtime Apr 21 05:27; log start Apr 20 16:41 | Norgate backtest written and run: `backtest.py --start 2024-01-01 --end 2026-04-21` on Windows with a Norgate trial. Output CSV: 1,289,872 rows. |
| Apr 21, 18:09 – 18:19 | prompt + script + report mtimes | Stratified-IC brief written, `analyze_backtest.py` produced, `analysis_2026-04-21.md` generated. Ten minutes between brief and report. |
| Apr 22, 18:19 – 18:44 | mtimes | Data-audit brief; `audit_extreme_returns.py`, `diagnose_history_gap.py`, `analyze_earnings_exclusion.py` and their three reports. |
| Apr 22, 19:09 – 22:38 | prompt mtime; log | Paper-trader brief written. At 22:38 an off-hours scan ran auto-trade: `candidates=50 opened=0 skipped=50` (entry-price fetch failed off-hours). |
| Apr 23 | `yfinance_adapter.py`, `db.py` mtimes; `parameter_history` rows 4–6 | Paper trader landed (`order_manager.py`, TP1 columns, `phase1_daily_review.py`). `auto_paper_trade`, `account_equity=26000`, `min_score=0.7` added to config. CONTEXT claims 333 tests. Scans Apr 22 (646 rows) and Apr 23 (323 rows). |
| Apr 24 – 29 | absence of any log or DB row | System dead for six days (Task Scheduler killed it on battery/idle per CONTEXT). |
| Apr 30 | 9 restarts; 8 new scripts; `CONTEXT.md` 23:31 | The "system was mostly dead" session: maintenance wiring, entry-price fallback, misfire grace, delay-score recompute, retail-attention always-numeric, UTC date fix, Task Scheduler XML. Ends with hand-launched daemon PID 25336 and the note that the watchdog is broken. |
| May 1 – 5 | logs | 7 `signal_log` rows on May 1 (smoke script, not a scan). APScheduler "missed by 22:57:24" warnings on May 2 and May 5: the laptop slept through every scheduled job. Effectively zero production between Apr 24 and May 5. |
| May 5, 21:17 – 21:21 | `.env.example` mtime; commit `1acd1a1` | `git init` and the "bleh" commit. |
| May 6, 01:24 – 02:05 | logs (backslash paths) | Four Windows starts overnight. |
| May 6, 08:56 – 09:11 | `.venv` mtime; logs (slash paths) | **Machine moved to Linux/Fedora.** New venv at 08:56, package installed 09:00. Daemon started at 09:03:36 **and again at 09:11:26 without stopping the first**. Both instances ran the 09:35 scan ("armed on first scan" logged twice, MNKD opened twice at different prices, two `auto_paper_trade` summary lines). VERIFIED from logs; not called out in DIAGNOSIS. |
| May 6, 10:37 – 13:10 | logs | Deadman fires in both instances (60-min timeout < 145-min gap to the 12:00 scan): stops widened at 10:47, 2 trades force-closed at 11:07. 12:00 scan opens 17 + 32 trades (the two instances). 13:00 deadman fires again; one instance shut down 13:01:19 and restarted 13:01:21; 13:10 stops widened on 49 open trades. |
| May 7 | logs, DB | 15:31 preclose scan opens 33 trades (the only preclose scan that ever wrote rows). 16:45 stops widened on 76 trades; 17:05 **76 trades force-closed with NULL exit price**. 6 trades stopped out at their (already widened) stops. Daemon restarted 03:13 and stayed up through May 12. |
| May 7 | `ic_history` | The single IC run ever performed: 15 rows, all negative or NULL, on the 500 labeled rows from the Apr 10–16 cohort. |
| May 11 – 12 | logs | Schwab refresh token dead: 539 batch-quote failures and 3,601 token POST failures; scans continue on yfinance only. Deadman force-closes 2 + 16 (May 11) and 5 (May 12). Last scan 09:35 ET May 12; last log line 11:29 ET May 12. |
| May 12 – Jun 9 | nothing | Four weeks of silence. |
| Jun 9, 12:40 – 15:47 | logs, `schwab_token.json` mtime 15:46 | Two Linux starts fail at Schwab token load ("Engine initialisation failed"); reauth at 15:46; third start succeeds and is Ctrl-C'd after 41 seconds. This is the last time the daemon ran. `arconian.db` mtime is 12:40 that day. |
| Jun 10 | `DIAGNOSIS.md` header | Read-only diagnostic audit: 36 findings, 6 blockers, fix plan awaiting approval. |
| Jun 23 – 24 | `FIXLOG.md`; 33 commits Jun 24 01:36–04:06 | Remediation session. Clusters A–E are dated Jun 23 in FIXLOG but committed after midnight; F–N are Jun 24. Work done on seven short-lived branches, each merged to `main` with a merge commit. |
| Jun 24, 04:06 | `cd7063c` | **Last commit.** |
| Jun 24 → Sep 20 | working tree | No commits, no daemon runs, no new DB rows. Working tree differs from HEAD only by line-ending changes in four analysis `.md` files *[corrected: the working-tree copies had CRLF; HEAD had LF]* (304 lines each way, no content change). The backtest CSV and `.cache/` directory, which FIXLOG says were "left on disk" when untracked, are no longer on disk. |

### 1.3 Branches

| Branch | State | What it was |
|---|---|---|
| `main` | HEAD `cd7063c` | Everything. |
| `fix/tier1-diagnosis-remediation` | exists, fully merged into `main` (`654580b`) | Repo hygiene + Tier-1 fixes + F-10/F-24/F-20. Not deleted. |
| `fix/tier2-f14-universe-isolation`, `fix/tier2-f14-f13`, `fix/tier2-batch2`, `fix/tier3-f17-risk-math`, `fix/tier3-risk-engine`, `fix/tier3-structural`, `fix/tier3-risk-enrichment`, `fix/tier3-engine-integration` | appear only in reflog; deleted after merge | Per-cluster remediation branches. Nothing abandoned: every branch in the reflog is an ancestor of `main`. |

There are no abandoned branches and no stashes. `git fsck` shows ten dangling blobs (unreferenced file versions from the amend at `98954ea`→`144423d` and staged-then-changed files); none represent lost work of note.

### 1.4 Distinct phases of work

1. **Apr 6:** Whitepaper and full Phase 0 codebase generated in one day.
2. **Apr 9 – 16:** Getting the daemon to score anything (universe seeding, restarts, first scans).
3. **Apr 20 – 22:** Backtest and the four analysis reports. Direction decision: proceed to Phase 1 despite negative after-cost backtest.
4. **Apr 22 – 23:** Paper trader built; auto-trading enabled.
5. **Apr 30:** Emergency repair of a system that had barely run.
6. **May 6 – 12:** The only week of paper trading. Data destroyed by the deadman switch and a duplicate daemon.
7. **Jun 9 – 10:** Restart attempt, then a full diagnostic.
8. **Jun 23 – 24:** Remediation of 27 of the 36 findings; risk engine wired but left off.
9. **Jun 24 → now:** Stopped.

### 1.5 The last meaningful commit

`5a188a8` / merge `65a23f0` ("RiskEngine read tp1_r_multiple from execution config + e2e test"). It added the first end-to-end test that drives the real `RiskEngine` through `auto_paper_trade_candidates`, which immediately found that shadow/enforce mode would have raised `AttributeError` on the first sized position. The commit after it (`cd7063c`) only records this in FIXLOG.

State it left things in: code compiles and the suite was green at 04:06 ET on Jun 24; `risk.full_engine_mode` is `"off"`; `signal.options_enabled` is `false`; deadman is `alert_only`; the May 6–12 trade cohort is still in the DB unmarked; the daemon has not been started since; no paper-trade clock restart has happened.

---

## 2. Stated goals vs. delivered

### 2.1 What it was supposed to do

From the whitepaper (§Abstract, §1, §11) and README: a rule-based system that scores U.S. small caps
($500M–$2B) with a five-signal composite for 1–3 day information-diffusion-lag trades, validated by
(a) a survivorship-bias-free Norgate backtest, then (b) ≥80 trading days / ≥40 round-trip paper
trades evaluated on gross expectancy >150 bps and net expectancy >0 after a 120/150 bps cost model,
then (c) live trading at 0.5% risk, with a risk engine (ATR sizing, regime gates, sub-industry and
Ledoit-Wolf correlation caps, CVaR stress, circuit breakers), corporate-action/earnings/macro
exclusions, a retail-attention penalty, IC-based weight recalibration every 60 days, triple-barrier
labeling feeding a Phase 3 meta-labeler, and a Zinniinae dashboard on the shared DB.

### 2.2 What the code does now

Component by component. "Wired" means reachable from `main.py` on the production scan path.

| Whitepaper component | Status today | Evidence |
|---|---|---|
| Universe manager, 5-state machine, monthly refresh, daily check | Built and wired; 326 tickers seeded Apr 10–13; daily check ran (last `last_checked` May 11). | DB, `universe_manager.py` |
| Six universe filters | Partly synthetic: spread from one pre-market quote, midday volume fabricated as 0.30 × daily, option-less names pass the OI filter (F-21). **Unfixed.** | DIAGNOSIS F-21; no FIXLOG entry |
| Hou-Moskowitz delay score | Built; **never computed in production until Apr 30** (zero call sites); positional-lag and inf→0.0 bugs (F-22) **unfixed**. | CONTEXT §5; DIAGNOSIS F-22 |
| Volume, return, sector-RS signals | Built and wired. Sector-RS was computed on misaligned windows in every scan through May 12 (F-8, fixed Jun 24). | signal_log, FIXLOG C |
| Options composite (15%) | Built; produced a **constant 0.5** on every ticker that passed the floors (281 rows) because no IV/PC history store exists; now **disabled** (`options_enabled: false`), weight redistributed. The IV/PC history store was never built. | DB, FIXLOG C, decision #6 |
| Retail attention penalty | NULL on every row until Apr 30; since then numeric but only the short-interest sub-component is live. StockTwits/Reddit `SocialAdapter` exists but is **never constructed in `main.py`**. Scanner flag has no data source. | CONTEXT §6, `main.py` |
| Directional confirmation (VWAP + return sign + options flow + borrow) | Reduces to sign of 1-day return: `prior_day_vwap` is NULL on all 4,247 rows (TODO still in code), options volumes never passed (F-18, deferred), borrow check not implemented. Ambiguous rate is 1.7% (73/4,247) vs the whitepaper's expected 30–50%. | DB, `signal_engine.py:571` |
| Earnings exclusion | `EarningsCalendar` existed but was **never instantiated** until Jun 24. All 4,247 rows are tagged `normal`. Wired now; has never run in production. | DB, FIXLOG C |
| Corporate-action exclusion (EDGAR) | Adapter exists, is passed into `SignalEngine`, and `self._edgar` is **never read**. Item map fixed Jun 24, still unwired. | `signal_engine.py:234`, FIXLOG I |
| Macro calendar blackout | Module exists; **no production importer**. | grep |
| Two-pass tiered scan | Built; Pass 2 gated off with options. | `signal_engine.py` |
| Paper trade state machine with TP1/trailing/time stop | Built and wired (Apr 23). Entry-day bar handling fixed Jun 24. | `order_manager.py` |
| Risk engine (sizing, regime, constraints, CB) | Built Apr 6, **dead code until Jun 24**; four math bugs fixed; now wired behind `full_engine_mode` which is `"off"`. Has never run in production. | FIXLOG J–N |
| CVaR + 4 stress scenarios | `risk/cvar.py` has **no production caller** even after wiring; sign and dimension bugs fixed in what remains dead code. | grep |
| Circuit breakers | Persisted and fed since Jun 24; only gate trades in `enforce` mode. Never run live. | FIXLOG K |
| Deadman switch | Built to whitepaper §6.3.2 (widen stops → force close); ran in production and **destroyed the dataset**; now `alert_only` with a 20-hour timeout. | logs, DB |
| Outcome collector / triple-barrier labels | Wired Apr 30; ran once (May 7) on 500 rows; used the fill price as the stop barrier (F-6, fixed). 3,747 rows still unlabeled. | DB |
| IC tracker | Ran once; segment mislabel and direction bug fixed Jun 24. | DB, FIXLOG H |
| Cost validator | Wrote |P&L| as slippage (F-10); now a modeled decomposition, explicitly not a measurement. No trade has cost columns populated. | DB (0 of 92 traded rows) |
| Parameter tracker | Self-corrupted after first config change (two `parameter_path=''` rows in DB); fixed Jun 24. | DB rows 3, 7 |
| `return_from_signal_time_3d`, `realized_pnl_after_tax`, partial-fill columns | Schema columns exist; **never computed** (F-34 unfixed). | DIAGNOSIS |
| Partial fill manager | 10-line stub. | file |
| Schwab order placement | Stubs that log a warning. | `schwab_adapter.py:619` |
| IC-based weight recalibration | Not implemented; `weight_*` columns store static config values. | code |
| ML pipeline (Phase 3) | Config keys only. | config |
| Norgate backtest | Script exists; requires Windows + NDU + `norgatedata`, none present. Cannot run here. | `norgatedata` import fails |
| Zinniinae integration | Page exists in the other repo, points at a hard-coded Windows user path (`C:\Users\<user>\...`). | grep |

### 2.3 Goals dropped, narrowed, or never reached

- **"Survivorship-bias-free backtest" was not delivered.** `backtest.py` pre-filters candidates with `_quick_prefilter(norgate, all_symbols, end_date)`, which loads only Apr 1–21 2026 data and keeps symbols with price ≥ $2 and 20-day ADTV ≥ $5M **as of the end of the window**. Delisted names return no data and are dropped; names that collapsed below $2 by April 2026 are dropped. The delisted universe Norgate was bought for was excluded by construction. The Apr 22 data-audit brief looked at exactly this code and wrote "That's fine for the prefilter." VERIFIED by code reading; the magnitude of the bias is NOT SHOWN (no run without the prefilter exists).
- **The backtest did not test the small-cap universe.** `MIN_MCAP_M`/`MAX_MCAP_M` are defined in `backtest.py` and never used; `_passes_filters` checks only history length, price ≥ $2 and ADTV ≥ $5M. The monthly universe was 3,094–4,097 tickers (whitepaper expects 150–300) and includes large caps (A, AA, AAL appear on 2024-11-01). Every backtest number in §3 describes essentially all liquid U.S. equities, not the thesis universe. The docstring's "market cap filter approximated" is not true of the code.
- **The Phase 1 gate was never reached.** ≥80 trading days / ≥40 clean round trips were required. Delivered: 5 trading days (May 6–12), 107 trades, 6 with a P&L, all 6 at widened stops and 3 of those duplicates.
- **"Never widen stops" (CONTEXT hard constraint #4)** conflicted with the whitepaper's deadman stage 2 and lost: stops were widened by 1 ATR on 2+2+49+76+2+16+5 occasions; trades 1–5 in the DB sit at 3.5× ATR (widened twice by the two instances).
- **Manual operator review** (whitepaper §11: "operator reviews the candidate, decides to take it or not") was replaced by unattended `auto_paper_trade` on Apr 23 with no position cap, no dedup and no risk engine, producing 51 opens on May 6 alone.
- **Options flow, retail attention, earnings/EDGAR/macro exclusions, directional confirmation** were all described in CONTEXT as "present in the live system" and used to argue the backtest was a lower bound. None of them was functioning in the live system at any point through May 12.
- **Paper-to-live parallel tracking, expectancy-gap decomposition, after-tax P&L, partial fills, cost measurement:** explicitly deferred in the Apr 22 brief; never picked up.
- **Options weight accelerated recalibration (30-day), IC weight adjustment with 0.05 floor, tiered risk, PEAD sub-model, hedging filter, 13F cross-reference, borrow availability:** never implemented.
- **Alembic as the only migration path** (supplements §3.5 rule 1): a single migration `0001`; later columns (TP1 fields) and the `circuit_breaker_state` table are created by `create_all` plus ad-hoc `ALTER TABLE` in `db.py`.
- **Windows Task Scheduler supervision** was abandoned with the Linux move; `run_arconian.sh` is a bare launcher with no supervisor.

---

## 3. Quantitative results, with provenance

Reproducibility key:

- **R1** Re-run today from the repo. The backtest CSV is not on disk but is recoverable from the private repository's history (137,121,407 bytes; built from licensed Norgate data and not distributed). I did this and re-ran `scripts/analyze_backtest.py` on 2026-09-20; the regenerated `analysis_2026-04-21.md` is byte-identical to the committed one (ignoring CRLF).
- **R2** Re-runnable but not stable: depends on live yfinance calls whose answers change over time.
- **R3** Not re-runnable: requires Norgate (`norgatedata` not installed; Windows + NDU + subscription; trial data was a rolling 2-year window so even a re-subscription would not return the same window).
- **R4** Read from `arconian.db` today (the DB is gitignored; it exists only on this disk).
- **R5** Read from logs today (gitignored; on this disk only).
- **R6** No reproducible source in the repo.

Unless stated, "the CSV" means `backtest_2024-01-01_2026-04-21.csv`: output of `scripts/backtest.py --start 2024-01-01 --end 2026-04-21`, run ~Apr 21 2026 on Windows against a Norgate trial (total-return adjusted) plus yfinance for SPY, sector ETFs and sector labels. Effective data window **2024-11-01 → 2026-04-15** (362 trading days). Signals: volume (30%), return magnitude (25%), options held at 0.50 (15%), sector RS (10%), delay (10%); no retail penalty; no earnings exclusion; forward return = close(t) → close(t+3 trading days); rows without a forward return dropped.

### 3.1 Backtest sanity and distribution (`analysis_2026-04-21.md`, Analysis 5)

| # | Number | Value | Appears in | Computed by | Method | Repro | Notes |
|---|---|---|---|---|---|---|---|
| 1 | Total rows | 1,289,872 | analysis md, CONTEXT ("1.29M"), stratified-IC brief | `analyze_backtest.py` on the CSV | `len(df)` | R1 (verified) | |
| 2 | Unique tickers | 4,251 | analysis md | same | `nunique` | R1 (verified) | Universe was not filtered to $500M–$2B (see §2.3). |
| 3 | Date range | 2024-11-01 → 2026-04-15 | analysis md, diagnose md | same | min/max | R1 (verified) | Requested start was 2024-01-01; end 2026-04-21 minus 3 forward days and weekend. |
| 4 | Rows with non-null delay | 867,707 (67.3%); 32.7% null | analysis md | same | count | R1 (verified) | Delay needs 52 weekly returns; with Norgate data starting 2024-04-22, delay is NULL for roughly the first five months of the window. |
| 5 | Composite percentiles p5/p25/p50/p75/p95 | 0.2504 / 0.3820 / 0.4929 / 0.6094 / 0.7531 | analysis md | same | `nanpercentile` | R1 (verified) | |
| 6 | Delay percentiles p5/p25/p50/p75/p95 | 0.0099 / 0.0730 / 0.1805 / 0.4197 / 0.9200 | analysis md | same | same, non-null rows | R1 (verified) | |
| 7 | Extreme forward returns | 123 > +100%; 1 < −90% | analysis md, data-audit brief ("124") | same | threshold count | R1 (verified) | |
| 8 | Composite range | [0.0924, 0.9138] | analysis md | same | min/max | R1 (verified) | |
| 9 | Overall mean / median 3-day forward return, all rows | +24.6 bps / +12.7 bps | **nowhere in docs** | my recomputation 2026-09-20 | mean, median | R1 | Added for context: the unconditional base rate the decile numbers should be read against. |

### 3.2 Composite decile lift (`analysis_2026-04-21.md`, Analysis 1)

Deciles by `pd.qcut(composite, 10)` over all 1,289,872 rows (pooled across dates and tickers, not per-day). Returns in bps.

| # | Decile | N | Mean | Median | Hit rate | SE | Repro |
|---|---|---|---|---|---|---|---|
| 10 | 1 | 128,988 | 17.3 | 8.5 | 51.7% | 1.4 | R1 (verified) |
| 11 | 2 | 128,987 | 15.8 | 9.2 | 52.3% | 1.4 | R1 |
| 12 | 3 | 128,987 | 19.3 | 11.3 | 52.9% | 1.5 | R1 |
| 13 | 4 | 128,987 | 22.0 | 11.6 | 53.2% | 1.4 | R1 |
| 14 | 5 | 128,987 | 23.9 | 12.6 | 53.5% | 1.6 | R1 |
| 15 | 6 | 128,988 | 27.4 | 13.4 | 53.8% | 1.6 | R1 |
| 16 | 7 | 129,754 | 26.5 | 13.3 | 53.9% | 1.6 | R1 |
| 17 | 8 | 128,219 | 30.3 | 16.4 | 54.5% | 1.6 | R1 |
| 18 | 9 | 128,987 | 32.7 | 16.7 | 54.4% | 1.7 | R1 |
| 19 | 10 | 128,988 | 30.5 | 15.8 | 53.6% | 2.1 | R1 |

| # | Number | Value | Appears in | Computed by | Method | Repro | Notes |
|---|---|---|---|---|---|---|---|
| 20 | Long-short spread D10 − D1 | +13.2 bps | analysis md, CONTEXT | `analyze_backtest.py` | difference of decile means | R1 (verified) | Decile 9 (32.7) beats decile 10 (30.5); lift is not monotone at the top. |
| 21 | Top-decile mean vs cost | +30.5 bps, "below §5 cost threshold of 120 bps" | analysis md, CONTEXT, memory notes | same | compare to constant `COST_BPS=120` | R1 (verified) | 120 bps is a whitepaper assumption, not a measurement. |
| 22 | Monotonicity Spearman r | +0.9636, p=0.0000 | analysis md, CONTEXT ("r = 0.96"), memory notes | same | `spearmanr(decile_index, decile_means)` over **10 points** | R1 (verified) | A rank correlation over ten aggregated means. The printed p-value is on n=10 and carries no information about the underlying 1.29M rows. |
| 23 | SE per decile | 1.4–2.1 bps | analysis md | same | `std/sqrt(n)` | R1 | Assumes independent observations; consecutive days of the same ticker share two of three forward-return days (see §4). |

### 3.3 IC by delay bucket (`analysis_2026-04-21.md`, Analysis 2)

Spearman IC of each column vs `fwd_return_3d`, pooled within bucket. "Clears §7.4" means IC ≥ 0.02.

| # | Bucket | n | composite IC (p) | volume IC (p) | return_mag IC (p) | sector_rs IC (p) | top-decile-within-bucket n / mean | Repro |
|---|---|---|---|---|---|---|---|---|
| 24 | A: delay > 0.5 | 179,345 | +0.0181 (0.0000) | +0.0205 (0.0000) clears | +0.0079 (0.0008) | +0.0006 (0.7933) | 17,935 / +49.0 bps | R1 (verified) |
| 25 | B: 0.3 < delay ≤ 0.5 | 117,233 | +0.0127 (0.0000) | +0.0144 (0.0000) | +0.0054 (0.0627) | −0.0004 (0.9017) | 11,724 / +55.8 bps | R1 |
| 26 | C: 0.1 < delay ≤ 0.3 | 288,687 | +0.0182 (0.0000) | +0.0202 (0.0000) clears | +0.0088 (0.0000) | +0.0020 (0.2930) | 28,869 / +59.5 bps | R1 |
| 27 | D: delay ≤ 0.1 | 282,442 | +0.0176 (0.0000) | +0.0156 (0.0000) | +0.0138 (0.0000) | +0.0010 (0.5884) | 28,245 / +58.4 bps | R1 |
| 28 | E: delay NULL | 422,165 | +0.0251 (0.0000) clears | +0.0160 (0.0000) | +0.0242 (0.0000) clears | +0.0065 (0.0000) | 42,162 / +7.3 bps | R1 |

| # | Number | Value | Appears in | Notes |
|---|---|---|---|---|
| 29 | "Thesis does NOT live" | Buckets A and B composite IC below 0.02 | analysis md headline, CONTEXT | The headline is generated automatically by the script from row 24–25. The high-delay buckets, which the thesis is about, have *lower* composite IC than the NULL-delay bucket. Bucket E is mostly the first five months of the window, so bucket and calendar period are confounded. |

### 3.4 Joint stratification (`analysis_2026-04-21.md`, Analysis 3)

| # | Subset | n | Mean | Median | Hit | SE | After cost (−120) | p5 / p25 / p50 / p75 / p95 (bps) | Repro |
|---|---|---|---|---|---|---|---|---|---|
| 30 | Top decile × delay > 0.3 ("thesis target") | 33,597 | +55.2 | +11.0 | 54.9% | 5.2 | **−64.8** | −878.9 / −177.0 / +11.0 / +233.6 / +1000.0 | R1 (verified) |
| 31 | Top decile, all delay | 128,988 | +30.5 | +15.8 | 53.6% | 2.1 | −89.5 | −890.8 / −212.2 / +15.8 / +251.1 / +929.4 | R1 |
| 32 | delay > 0.3, all deciles | 296,578 | +35.1 | +7.9 | 54.1% | 1.2 | −84.9 | −715.7 / −161.6 / +7.9 / +198.2 / +818.5 | R1 |

Row 30 is the number the whole Phase 1 decision was argued from (CONTEXT "The Decision"). Note the
thesis-subset median (+11.0) is *below* the top-decile median (+15.8): adding the delay filter lowers
the typical outcome and raises only the mean, via the tail.

### 3.5 Time stability (`analysis_2026-04-21.md`, Analysis 4)

Thirds by row count, not by calendar.

| # | Period | Dates | Composite IC | Top-decile mean | Top×Delay mean | Repro |
|---|---|---|---|---|---|---|
| 33 | Early | 2024-11-01 → 2025-05-12 | +0.0095 | +7.5 bps | +85.4 bps | R1 (verified) |
| 34 | Middle | 2025-05-13 → 2025-11-04 | +0.0046 | +51.4 bps | +54.5 bps | R1 |
| 35 | Late | 2025-11-05 → 2026-04-15 | +0.0299 | +59.7 bps | +53.9 bps | R1 |
| 36 | Verdicts | "IC rising"; "Top×Delay declining"; headline "signal may be in decay" | analysis md, CONTEXT ("a puzzle worth monitoring") | Script-generated from thresholds of 0.005 IC and 5 bps. The headline says "decay" because `A4_is_stable` is False, even though IC rose; the wording is a template artifact. Delay is NULL for most of the Early third, so the Early "Top×Delay" n is small (NOT SHOWN in the report). |

### 3.6 Aggregate IC (stated only in `claude-code-prompt-stratified-ic.md`)

These were printed to stdout by `backtest.py` (`compute_ic_summary`) when the backtest ran; the output was never saved. The brief quotes them from memory. I recomputed them from the recovered CSV with the same function logic.

| # | Number | Value in doc | My recomputation (2026-09-20) | Repro | Notes |
|---|---|---|---|---|---|
| 37 | Composite aggregate IC | +0.013 | +0.0131 (n=1,289,872, p≈3e-50) | R1 | R6 as documented; R1 after recovery. |
| 38 | Delay aggregate IC | −0.024 ("wrong sign") | −0.0243 (n=867,707) | R1 | The one signal that is the thesis has negative pooled IC. Never revisited after the stratified analysis reframed it as "a filter, not a signal". |
| 39 | Volume IC | "below 0.02" | +0.0163 | R1 | |
| 40 | Return-magnitude IC | "below 0.02" | +0.0134 | R1 | |
| 41 | Sector-RS IC | "below 0.02" | +0.0028 (p=0.001) | R1 | Economically zero. |
| 42 | Per-day cross-sectional composite IC | **not computed by the project** | mean −0.0028, sd 0.0747, 362 days, t = −0.72, 49% of days positive | R1 (mine) | Spearman within each date, then averaged. The whitepaper's IC definition is pooled, and the project only ever computed pooled IC. Cross-sectionally, day by day, the composite does not rank stocks. I did not decompose why the pooled IC is positive while the daily IC is zero; the obvious candidate is common time variation (periods where volume percentiles and subsequent returns were both high). |

### 3.7 Extreme-return audit (`audit_extreme_returns_summary.md`)

Script `scripts/audit_extreme_returns.py`, run Apr 22 2026 on Windows. Input: the CSV plus Norgate price history per extreme row (14 days before to 30 days after). Classification: `real_move` if the recomputed close-to-close 3-day return matches the CSV within ±5 percentage points; `split_artifact` if not and a clean split ratio is seen; else `computation_mismatch`. The per-row output `audit_extreme_returns.csv` is retained in the private repository's history (Norgate-derived; not distributed).

| # | Number | Value | Repro | Notes |
|---|---|---|---|---|
| 43 | Extreme observations (abs > 20%) | 12,950 | R1 for the count (verified); R3 for classification | CONTEXT's "1.0% of dataset" = 12,950 / 1,289,872 = 1.004%. |
| 44 | real_move | 12,948 (100.0%) | R3 (audit CSV recoverable, verified counts) | "Confirmed by Norgate" means the same Norgate series the CSV was built from reproduces the arithmetic. It is a self-consistency check, not independent validation. The brief's "smooth daily transitions" criterion was not implemented; a large gap only adds a note. |
| 45 | computation_mismatch | 2 | R3 | Not investigated further. |
| 46 | split_artifact / norgate_lookup_failed | 0 / 0 | R3 | |
| 47 | Top 10 moves | Ten moves between +305.2% and +613.6% across six tickers *[per-ticker values omitted from the public snapshot: licensed Norgate data]* | R1 for values (in the CSV) | CONTEXT's attributions ("Capricor, DMD", "Dermavant", "FDA catalyst") are DOC-ONLY; nothing in the repo looked up what these companies did. |
| 48 | Recomputed means with 2 artifacts removed | thesis +55.2 → +55.2; top decile +30.5 → +30.5; bottom decile +17.3 → +17.4 | R1 in principle (drop two rows) | Verdict "YES, means reliable" is auto-generated because real_move ≥ 85%. |

### 3.8 History-gap diagnosis (`diagnose_history_gap.md`)

Script `scripts/diagnose_history_gap.py`, Apr 22, Norgate.

| # | Number | Value | Repro | Notes |
|---|---|---|---|---|
| 49 | Universe Jan–Oct 2024 | 0 tickers | R6 (from the original backtest log, not saved) | Consistent with the CSV starting 2024-11-01. |
| 50 | Universe Nov 2024 | 3,094 tickers | R1 (verified: 3,094 rows on 2024-11-01) | Monthly universe thereafter 3,318 → 4,097 (my count). |
| 51 | Sample tickers' Norgate coverage | Five sample tickers: 136 rows each, 2024-04-22 → 2024-11-01 | R3 | Norgate trial = rolling 24 months from the run date. |
| 52 | Effective window | Nov 2024 → Apr 2026, "~17 months, ~365 trading days" | R1 | 362 trading days in the CSV. |
| 53 | Norgate Platinum | "~$500/year" | DOC-ONLY | Whitepaper §2.4 assumption. |

### 3.9 Earnings exclusion and return caps (`analysis_earnings_exclusion.md`)

Script `scripts/analyze_earnings_exclusion.py`, Apr 22. Input: the CSV plus `yf.Ticker(t).earnings_dates` for the 1,799 thesis tickers, fetched live that day. Proximity = within ±3 *calendar* days of any earnings date yfinance reported as of Apr 2026 (retrospective dates; no point-in-time calendar).

| # | Number | Value | Repro | Notes |
|---|---|---|---|---|
| 54 | Thesis tickers with yfinance earnings data | 1,371 / 1,799 (76%) | R2 | |
| 55 | Thesis rows within ±3 cal days of earnings | 1,017 (3.0%) | R2 | |
| 56 | Thesis rows with unknown earnings date | 9,471 (28.2%) | R2 | Unknowns are kept in the "excluded" subset. |
| 57 | Return-cap sensitivity, thesis subset: uncapped / ≤200% / ≤100% / ≤50% / ≤20% | mean +55.2 / +45.8 / +42.2 / +35.1 / +23.0; median +11.0 / +11.0 / +10.9 / +10.8 / +10.1; n 33,597 / 33,587 / 33,577 / 33,493 / 32,819 | R1 (Part 1 uses only the CSV) | CONTEXT: "the typical signal produces ~+10 bps, one-twelfth of the cost threshold." |
| 58 | Same, top decile all delay | +30.5 / +28.1 / +26.1 / +21.6 / +14.6 (means); medians +15.8 / +15.8 / +15.8 / +15.6 / +14.9 | R1 | |
| 59 | Same, bottom decile | +17.3 / +17.1 / +16.8 / +15.4 / +11.6; medians +8.5 / +8.5 / +8.4 / +8.4 / +8.1 | R1 | Bottom-decile median +8.1 vs thesis median +10.1 at the ±20% cap: the thesis filter adds about 2 bps of typical return over the *worst* decile. |
| 60 | Combined: original | n 33,597, +55.2 / +11.0, after cost −64.8 | R1 | |
| 61 | Earnings excluded (unknowns kept) | n 32,580, +52.9 / +10.4, −67.1 | R2 | |
| 62 | Earnings excluded (confirmed coverage only) | n 23,109, +59.3 / **+26.5**, −60.7 | R2 | CONTEXT flags the 2.5× median jump as "not yet actionable but worth watching"; never followed up. |
| 63 | Earnings excl + ≤100% | n 32,560, +39.5 / +10.3, **−80.5** | R2 | The number CONTEXT bolds as the cleaned thesis result. |
| 64 | Earnings excl + ≤50% | n 32,477, +32.4 / +10.2, −87.6 | R2 | |
| 65 | Earnings excl + ≤20% | n 31,852, +21.9 / +9.9, −98.1 | R2 | |

### 3.10 Live signal data (`arconian.db`, `signal_log`)

| # | Number | Value | Appears in | Repro | Notes |
|---|---|---|---|---|---|
| 66 | Rows Apr 10–23 | 1,656 on 5 days, all `scan_type='open'` | CONTEXT, data-quality brief | R4 (verified: 14+27+646+646+323) | The "known-bad cohort" per CONTEXT. |
| 67 | Candidates clearing 0.70 in that cohort | 249 | CONTEXT | R4 (verified: 0+3+110+86+50) | Zero were traded. |
| 68 | Composite distribution of that cohort | median 0.45, p95 0.82, max 0.97 | data-quality brief | R4 (verified) | |
| 69 | Total rows | 4,247 (2,955 open, 969 midday, 323 preclose) | DIAGNOSIS | R4 (verified) | Last row 2026-05-12 09:35 ET. |
| 70 | `return_3d` NULL | 3,747 of 4,247 | DIAGNOSIS, memory | R4 (verified) | Labeled rows are exactly the Apr 10–16 cohort (14+27+459). |
| 71 | Outcome labels | +1: 211, −1: 195, 0: 94 | nowhere | R4 | Produced with the fill-price-as-stop bug (F-6) and 2-bar premature labeling (F-11) for untraded rows the ATR proxy applied; not trustworthy. |
| 72 | Mean raw `return_3d` on the 500 labeled rows | +2.18% (min −24.7%, max +44.3%) | nowhere | R4 | Apr 10–16 2026; long convention regardless of direction. |
| 73 | `options_composite` | 0.5 on 281 rows, NULL on 3,966 | DIAGNOSIS (F-4) | R4 (verified) | Constant, hence IC NULL. |
| 74 | `earnings_proximity_tag` | `normal` on all 4,247 | DIAGNOSIS (F-5) | R4 (verified) | Exclusion never fired. |
| 75 | `prior_day_vwap`, `vix_level` | NULL on all 4,247 | DIAGNOSIS (F-18) | R4 (verified) | |
| 76 | `direction_signal` | bullish 2,335; bearish 1,838; ambiguous 73 (1.7%); NULL 1 | nowhere | R4 | Whitepaper §3.4 expected 30–50% ambiguous. |
| 77 | `delay_score_as_of_signal` populated | 0 before May 1; 1,272/1,292 May 6; 318/323 May 7; 636/646 May 11; 318/323 May 12 | CONTEXT (fix #5) | R4 (verified) | |
| 78 | `retail_attention_score` populated | 0 before May 1; all rows after | CONTEXT (fix #6) | R4 (verified) | SI-only; social never wired. |
| 79 | `history_status='short'` rows | 24 | DIAGNOSIS (F-20) | R4 (verified: 2+2+1+8+2+6+3) | |
| 80 | Smoke examples | ABR delay 0.0378, ABUS 0.6735 | CONTEXT | R4 possible; not checked | |
| 81 | Universe | 326 tracked: 322 ACTIVE, 1 OBSERVATION, 3 REMOVED; 318 with delay score (as of 2026-04-30) | CONTEXT | R4 (verified) | Whitepaper expects 150–300; seeding without Schwab was "permissive". |

### 3.11 Live IC (`ic_history`)

Single run, written by `journal/ic_tracker.py` during the May 7 16:15 maintenance, on all rows with `return_3d` (the 500 Apr 10–16 rows), window 2025-05-07 → 2026-05-07, Spearman pooled, raw long-convention returns (the direction flip came Jun 24).

| # | Component | IC | n | Repro | Notes |
|---|---|---|---|---|---|
| 82 | composite | **−0.1086** | 500 | R4 (verified) | Also cited in DIAGNOSIS as "−0.109 on the small contaminated sample". |
| 83 | volume | −0.0275 | 500 | R4 | |
| 84 | return | −0.0902 | 500 | R4 | |
| 85 | sector_rs | −0.0433 | 500 | R4 | Sector RS was misaligned (F-8) in these rows. |
| 86 | options | NULL | 87 | R4 | Zero variance (constant 0.5). |
| 87 | Segments | `all`, `open_scan`, `normal` rows are identical | R4 | Because every labeled row is an open scan tagged normal. `computed_at` holds the period start (2025-05-07), not the run time (F-34, unfixed). |

These are the only live IC numbers the project ever produced. They are negative, computed on a cohort the author had already declared known-bad, with two of five signals broken or constant. They have no evidential weight in either direction.

### 3.12 Paper trades (`trades`)

| # | Number | Value | Appears in | Repro | Notes |
|---|---|---|---|---|---|
| 88 | Trades | 107, entries May 6 (51), May 7 (33), May 11 (18), May 12 (5) | DIAGNOSIS | R4 (verified) | |
| 89 | Force-closed with NULL exit/P&L | 101 (2 + 76 + 2 + 16 + 5 by day) | DIAGNOSIS, memory | R4 + R5 (verified; log counts match) | |
| 90 | Trades with a P&L | 6, all `stop`, all closed 2026-05-07 | DIAGNOSIS | R4 (verified) | MNKD ×3 (ids 6, 10, 12: identical entry 4.00, identical exit 3.5338, identical P&L −216.34) and AOSL ×3 (−208.17 each). |
| 91 | Sum / mean realized P&L | −$1,273.53 / −$212.26 | nowhere | R4 | Three distinct outcomes counted six times; stops were at 2.5× ATR (widened from 1.5×). Meaningless as expectancy. |
| 92 | Same-ticker pyramiding | MNKD ×5; ATEC, NNE, NVAX, VRDN ×4 | DIAGNOSIS | R4 (verified) | |
| 93 | Exact duplicate (ticker, entry price, entry time) groups | 10 | nowhere | R4 | Two daemon instances on May 6. |
| 94 | Stop distance on trades 1–5, 7–9, 11 | 3.5 × ATR (2.5 × on 6, 10, 12) | nowhere | R4 | Entered at 1.5×; widened by +1 ATR once or twice. Direct DB evidence of F-2. |
| 95 | Cost columns on traded signal rows | 0 of 92 populated | nowhere | R4 | Cost validator never had a trade with an exit price to work on. |

### 3.13 Operational counts (logs)

| # | Number | Value | Repro | Notes |
|---|---|---|---|---|
| 96 | Daemon starts, Apr 6 – Jun 9 | 57 | R5 | 9 on Apr 30 alone. |
| 97 | Days on which scans wrote rows | 10 (Apr 10, 13, 16, 22, 23; May 1 smoke; May 6, 7, 11, 12) | R4 | Against a plan of ≥80 trading days. |
| 98 | Deadman TIMEOUT escalations | 11 (Apr 13, Apr 14 ×2, May 6 ×4, May 7, May 11 ×2, May 12) | R5 | Force-close reached 6 times. |
| 99 | Stops widened (events × trades) | 0, 0, 2, 2, 49, 76, 2, 16, 5 | R5 | |
| 100 | Schwab failures May 11–12 | 539 `get_quotes` batch failures; 3,601 token-refresh POST 400s | R5 | Token expiry never alerted (F-13, fixed Jun 24). |
| 101 | Scan gaps that exceeded the 60-min deadman | 09:35→12:00 = 145 min; 12:00→15:30 = 210 min | DIAGNOSIS ("131–210 min") | R5 | |

### 3.14 Test-suite counts (all DOC-ONLY except the last row)

| # | Date | Count | Source |
|---|---|---|---|
| 102 | Apr 10 | 316 pass | seed-universe brief |
| 103 | Apr 23 | 333 pass, "2 pre-existing deadman failures" | CONTEXT |
| 104 | Apr 30 | 359/363 → 361/363 | CONTEXT |
| 105 | Jun 10 | 361 passed / 2 failed | DIAGNOSIS (the 2 were deterministic, F-31) |
| 106 | Jun 23–24 | 366, 373, 386, 390, 394, 406, 409, 426, 445, 463, 478, 488, **491 passed / 0 failed** | FIXLOG clusters A–N |
| 107 | **Sep 20 (today)** | **491 collected; 490 passed, 1 failed** (`TestScanSchedulerMarketGuard::test_scan_runs_on_open_market_day`) | executed | The F-26 early-close guard added on Jun 24 compares the *real* wall clock against a 16:00 ET close even when `is_market_open` is mocked, so the test fails whenever the suite runs after 16:00 ET. FIXLOG's green run was at ~04:00 ET. Code-reading conclusion, not re-run during market hours. |

### 3.15 Repo-hygiene numbers (FIXLOG, all VERIFIED)

| # | Number | Value |
|---|---|---|
| 108 | Cache JSONs untracked | 2,272 (2,278 files changed in `481f2d9` including the two CSVs, `.bat`, `.sh`, `.gitignore`, `pyproject`) |
| 109 | Backtest CSV size | 137,121,407 bytes ("132 MB" in FIXLOG is MiB-ish: 130.8 MiB) |
| 110 | Audit CSV size | 1,153,887 bytes |
| 111 | `.git` size today | 131 MB (the CSV lives on in history) |
| 112 | Deadman timeout | 60 → 1,200 min |
| 113 | DeprecationWarnings | "~370 → 0" (DOC-ONLY; not re-counted) |

### 3.16 Numbers that appear in docs with no source in the repo

- Whitepaper effect sizes (0.33%/month, 58% decay, 1.8%/month alpha, 120/150 bps costs, 100–200 bps gross edge, 150 bps minimum, 45–52% win rate, etc.) are literature citations and design assumptions. None was measured here.
- "Scans 3–5 minutes", "8–15 candidate events per day", "150–300 names": design estimates. Actual: 323 names; scans took ~2–3 minutes (09:35:00 → 09:37:44); candidate counts above 0.70 ranged 0–110 per scan.
- CONTEXT's biotech/FDA attributions for the tail names.
- "Maximum expected loss $1,500–$3,000" (whitepaper §11): never tested.
- The Apr 22 auto-trade `candidates=50 opened=0` is in the log (R5) but the reason CONTEXT gives (off-hours `fast_info` None) is inferred; `debug_zero_trades.py` exists to test it but its output was not saved.

---

## 4. Methodology audit

### 4.1 What validation was actually performed

1. **One pooled historical backtest** over a single 17-month window, with the composite computed as the live code would (same `compute_composite`, same signal functions), scored against 3-day close-to-close returns. No parameter was fit to this data: weights, thresholds (0.70, delay 0.3), cost (120 bps) and the IC bar (0.02) were all fixed a priori by the whitepaper. That is the strongest thing that can be said for it.
2. **Stratified descriptive analysis** of that one run: deciles, delay buckets, one joint subset, thirds by time.
3. **A self-consistency audit** of extreme returns against the same data source.
4. **A sensitivity table** for return caps and a retrospective earnings-proximity filter.
5. **Unit tests** (491) that exercise arithmetic and control flow with mocks. No test touches market data.
6. **Five trading days of unattended paper trading** whose outcomes were destroyed.

Not performed: any holdout or out-of-sample split; walk-forward or rolling re-estimation (the backtest is walk-forward in the sense that signals use only past data, but nothing is *fit* and re-tested); permutation or bootstrap tests; block-bootstrap standard errors; baselines (random selection, a plain volume-only rule, or the market); cross-sectional (per-day) IC; a run with the full small-cap filters; any comparison of paper fills to a benchmark; any live IC on clean data.

### 4.2 Known or likely weaknesses

**Survivorship bias, reintroduced.** `_quick_prefilter` selects the symbol set using only the last three weeks of the window (Apr 1–21 2026). Delisted names have no rows there and are excluded; names below $2 or $5M ADTV in April 2026 are excluded. The backtest that exists is therefore a backtest of names that were alive and liquid at the *end*. The direction of the bias is to overstate forward returns, most strongly in exactly the high-volume, high-return-magnitude, small, volatile names the composite ranks highest. The whitepaper calls the delisted-inclusive universe "non-negotiable"; the data-audit brief inspected this function and waved it through. VERIFIED (code). Magnitude NOT SHOWN.

**Wrong universe.** No market-cap bound is applied (constants defined, never used), so ~3,100–4,100 names per month were tested rather than 150–300 small caps. All §3 backtest numbers are about a different population than the one the system trades. VERIFIED (code + universe sizes).

**Non-independent observations and inflated n.** Every ticker contributes an observation every trading day, and consecutive days' 3-day forward returns overlap by two days. The 1.29M "n" and the printed SEs (1.4–5.2 bps) and p-values (0.0000) treat these as independent. Effective sample size is far smaller and the SEs are understated by an unknown factor. No block bootstrap or Newey-West correction anywhere.

**Pooled vs cross-sectional IC.** Every IC in the project is pooled across dates. My per-day cross-sectional IC of the composite averages −0.003 across 362 days (t ≈ −0.7). Whatever produces the pooled +0.013 and the decile lift, it is not the composite ranking stocks against each other on a given day. This is the single most important thing the analysis did not check.

**Delay buckets confound with time.** Delay requires 52 weekly returns and the trial data starts 2024-04-22, so delay is NULL for most rows before ~April 2025. "Bucket E" and "Early third" largely overlap. The stability analysis's Early-period "Top×Delay +85.4 bps" rests on the few rows that had a delay score in that period; the report does not print that n.

**Multiple comparisons.** Five signals × five buckets, three subsets, three periods, fifteen cap/subset cells and six filter combinations were examined with no correction, and the headline was written by choosing the subset (top decile × delay > 0.3) that best fits the thesis. The two "clears §7.4" hits in the bucket table (volume in A and C) are what you would expect from 20 comparisons of near-zero correlations with enormous n.

**Monotonicity statistic.** A Spearman r of +0.96 over ten decile means is presented (in CONTEXT and in the memory notes) as "the thesis has real information content." It is a rank correlation of ten aggregated points whose spread (13 bps) is about ten times the stated SE; deciles 9 and 10 are inverted; and the same-direction pooled effect could arise from time effects alone. It does not establish cross-sectional predictive power.

**Cost threshold arithmetic.** "After cost" is `mean − 120`. The 120 bps is the whitepaper's working assumption; nothing measured costs. Under the whitepaper's own lower cost estimate (75 bps) the thesis subset is still negative (+55.2 − 75).

**Signal timing mismatch.** The backtest computes each day's signal from that day's full-session volume and close and enters at that close. The live system scans at 09:35 (five minutes of volume), 12:00 and 15:30, and the entry-price path uses a live quote. Live signals and backtest signals are different quantities; live IC on the 500 labeled rows was computed against close-to-close returns from the *scan date*, which for a 09:35 scan includes the rest of that day.

**Sector labels are not point-in-time.** The backtest maps each symbol to a sector ETF via today's yfinance `.info`.

**Extreme-return audit is circular.** It confirms that the CSV's forward return equals a recomputation from the same Norgate series. It cannot detect an adjustment error in Norgate itself, and it did not implement the "no unexplained single-day gap" rule the brief asked for. "100% real moves" should be read as "100% arithmetically consistent."

**Earnings filter is retrospective and partial.** yfinance earnings dates as of Apr 2026, 24% of tickers unknown and kept, ±3 calendar days as a proxy for ±1 trading day. The finding that only 3% of thesis rows are earnings-proximate should be treated as a lower bound.

**Live data is unusable as evidence.**
- Apr 10–23 cohort: no delay score, no retail penalty, misaligned sector RS, constant options, labels computed with the wrong stop barrier. The author marked it known-bad on Apr 30.
- May 6–12 cohort: duplicate daemon, stops widened, 101 of 107 trades with no exit, 3 unique outcomes. The remediation decision (#3) was to mark it known-bad and restart the clock. **The marking has not been done**: there is no cohort flag column, no override row, no note in the DB.
- The one `ic_history` run is on the first cohort.

**Test suite as evidence.** The suite is green (modulo the wall-clock test) but every external dependency is mocked. Several production-only failures (F-7 pandas-3 crash, F-8 misalignment, F-9 length mismatches, the deadman cadence, duplicate instances, token expiry) were invisible to it. A green suite here means the arithmetic matches the author's intent, not that the system works.

### 4.3 Things flagged by the author and not fixed

| Flag | Where | Status now |
|---|---|---|
| "Rising IC + declining returns in the thesis subset is a puzzle worth monitoring" | CONTEXT, Apr 30 | Never revisited. |
| "Confirmed earnings coverage only" median 2.5× higher, "may be a yfinance coverage artifact" | CONTEXT | Never revisited. |
| Delay aggregate IC negative, "wrong sign" | stratified-IC brief | Reframed as "filter not signal"; the negative pooled IC itself never explained. |
| Prefilter uses `end_date − 40 days` ("that's fine for the prefilter") | data-audit brief | Wrong call; see §4.2. |
| Backtest "market cap filter approximated" | `backtest.py` docstring, CONTEXT | Not approximated; absent. |
| Options signal "the critical missing piece" | CONTEXT | Was constant 0.5 in live data for six weeks (F-4); now disabled. Store never built. |
| `SocialAdapter` not wired; needs top-40 refactor | CONTEXT open question #3 | Still not wired. |
| `prior_day_vwap` from Schwab 5-min bars | `signal_engine.py:571` TODO, `universe_manager.py:677` TODO | Still TODO. |
| Retail weight 0.20 vs 0.10 | CONTEXT open question #4 | Fixed to 0.10 Jun 24. |
| Schwab weekly token, no automation | CONTEXT open question #5 | Detection fixed (F-13); still manual. |
| F-21 universe filters synthetic/vacuous | DIAGNOSIS | Unfixed. |
| F-22 delay-score lag gaps, inf → 0.0 written to DB | DIAGNOSIS | Unfixed. Affects the thesis's defining filter. |
| F-28 cached ETF frame serialization on pandas 3 | DIAGNOSIS | Unfixed. |
| F-30 suspension reason overwritten | DIAGNOSIS | Unfixed. |
| F-34 `computed_at` repurposed; `return_from_signal_time_3d` never computed | DIAGNOSIS | Unfixed. |
| F-18 chain volumes never reach directional confirmation | DIAGNOSIS | Deferred as moot while options are off; the confirmation layer is still the sign of a one-day return. |
| "Monthly refresh shares the long-transaction shape" | FIXLOG G | Left as-is. |
| Deadman "coarse backstop, not per-scan" at 20 h | FIXLOG A | Accepted trade-off; the switch no longer detects an intraday crash. |
| Decision #3 "mark May 6–12 cohort known-bad" | FIXLOG summary | "Was not performed." Still not performed. |

---

## 5. Current state

**Is this done?** No. Measured against its own plan it is at the start of Phase 1 with zero usable Phase 1 data, a backtest that tested the wrong universe with survivorship bias, and a daemon that has not run since Jun 9.

### 5.1 Works end to end (as far as the repo shows)

- `config` loading with required-key validation; DB init in WAL mode with idempotent column migrations.
- `scripts/analyze_backtest.py` on the git-recovered CSV (verified identical output today).
- Part 1 of `analyze_earnings_exclusion.py` (CSV-only); Parts 2–3 with live yfinance.
- The unit-tested paths: composite math, signal functions, universe state machine, paper-trade exit walk, triple-barrier labeling with the real stop, circuit-breaker persistence, risk-engine sizing and constraints (via stubs and one e2e test with the real config).
- `python main.py --dry-run` is documented to run one scan and exit; NOT SHOWN to work on this machine since Jun 9 (Schwab token from Jun 9 is expired; yfinance-only path untested since).

### 5.2 Half-finished

- Risk engine: wired, tested with stubs, `full_engine_mode: "off"`. Shadow mode has never been observed on real scans.
- Earnings exclusion: wired Jun 24, never run.
- Options signal: disabled pending an IV/PC history store that does not exist.
- Retail attention: SI component only.
- Directional confirmation: return sign only.
- Outcome labeling: correct now, but 3,747 rows remain unlabeled and the two bad cohorts are unmarked.
- Cost validation: a model with no fills to validate.
- Supervision on Linux: `run_arconian.sh` only; no systemd unit, no watchdog, no single-instance lock (the May 6 duplicate could recur).

### 5.3 Broken

- One test fails after 16:00 ET (wall-clock dependence in the F-26 guard test).
- `schwab_token.json` (Jun 9) is expired; every Schwab call will short-circuit with one CRITICAL alert.
- `scripts/backtest.py`, `audit_extreme_returns.py`, `diagnose_history_gap.py`: `norgatedata` not installed and not installable on Linux.
- `../Zinniinae/pages/09_arconian_live.py`: Windows path.
- `scripts/arconian_task.xml`, `setup_task_scheduler.ps1`: Windows-only, still referenced by CONTEXT.
- `CONTEXT.md` describes a Windows host, a running daemon, 361/363 tests, retail weight 0.20, and the deadman as designed. Most of it is stale.
- The four analysis `.md` files show as modified in `git status` due to CRLF; harmless but noisy.

### 5.4 Dead code

- `risk/cvar.py`: no production callers, even after its math was fixed.
- `execution/partial_fill_manager.py`: 10-line stub.
- Schwab order-placement methods: stubs that log a warning.
- `data/macro_calendar.py`: no production importers.
- EDGAR in the signal engine: `self._edgar` is assigned and never read.
- `data/norgate_adapter.py`: cannot import on this platform.
- `scripts/verify_entry_price_fix.py`, `verify_maintenance_wiring.py`, `smoke_data_quality_fix.py`, `debug_zero_trades.py`: one-off scripts tied to the Apr 30 state.
- Schema columns never written: `return_from_signal_time_3d`, `realized_pnl_after_tax`, `partial_fill_flag`, `fill_pct`, `borrow_available`, `cost_borrow_bps`.
- The startup banner still says "Phase 0 — signal scoring only" while `auto_paper_trade` is on.

### 5.5 Size

Roughly 12,000 lines of Python outside tests (signals 1.7k, universe 1.1k, risk 1.6k, execution 1.9k, journal 1.5k, data 2.4k, config 0.5k, scripts 3.4k, root 1.2k) and 6,600 lines of tests.

---

## 6. Unresolved threads

Things the evidence says you were in the middle of, or left open, in rough priority order:

1. **Mark the two bad cohorts and restart the paper-trade clock** (decision #3). Not done. There is no mechanism for it; a cohort/quality column on `signal_log` and `trades`, or an `overrides`-style table, would need to be added first.
2. **Decide whether the backtest is worth redoing correctly.** Two defects (end-of-window prefilter; no market-cap filter) mean no existing number describes the thesis universe without survivorship bias. A correct rerun needs Norgate on Windows and a per-date candidate set. The negative per-day cross-sectional IC (row 42) should be checked first, on the recovered CSV, before spending money on data.
3. **Flip `full_engine_mode` to `shadow` and watch it** for a soak period, then `enforce`. The FIXLOG plan says shadow-first; it never started.
4. **Options IV/PC history store** (Phase-2 project per decision #6). Without it the 15% signal stays off and Pass 2 never runs.
5. **Wire `SocialAdapter`** (needs the top-40-only refactor noted Apr 30) and a real `prior_day_vwap` (Schwab 5-min bars, TODO since Apr 6), or accept that directional confirmation is a sign test and say so in the docs.
6. **Wire EDGAR corporate-action detection and the macro calendar**, now that their latent bugs are fixed, or delete them.
7. **F-21, F-22, F-28, F-30, F-34** from DIAGNOSIS: unaddressed. F-22 (delay score) matters most for the thesis.
8. **Process supervision on Linux**: single-instance lock, restart-on-crash, token-expiry alert routing. The May 6 duplicate-daemon incident has no guard against recurrence.
9. **Fix the wall-clock-dependent test**, and consider a smoke test that runs the real scan path against recorded yfinance responses, since every production failure so far was invisible to the mocked suite.
10. **Rewrite or retire `CONTEXT.md`**; it is the file a new session is told to read first and it describes a system that no longer exists.
11. **Delete the merged `fix/tier1-diagnosis-remediation` branch**; decide whether the 131 MB `.git` (the CSV in history) is acceptable or should be rewritten.
12. **Zinniinae page path** and whether the shared-DB design survives the Linux move.
13. **Norgate**: subscribe (Platinum, Windows host) or abandon historical validation. The trial's 24-month window is the reason the backtest starts in Nov 2024.
14. Open questions carried from CONTEXT that were never answered: Roth IRA vs taxable (affects the success criteria), and the earnings-coverage median anomaly.

Nothing in the repo indicates work after 2026-06-24 04:06 ET.
