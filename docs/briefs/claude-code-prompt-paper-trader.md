# Claude Code prompt: Implement Phase 1 paper trader

## Context

You are implementing the Phase 1 paper trading workflow for Arconian. Read these first before touching code:

1. `CONTEXT.md` — current state of the system, backtest findings, and the explicit decision to proceed to Phase 1.
2. `arconian-v3.md` §11 "Phase 1" — this is the spec. Read especially the "Paper-to-Live Transition Framework" subsection.
3. `arconian-supplementary-docs.md` Doc 2 §2.3–2.5 (risk, execution, cost params) and Doc 3 §3.3 (NULL propagation).
4. `models.py` — the `Trade` and `SignalLog` ORM definitions. `Trade` already exists; `trade_id` on `SignalLog` already links them.
5. `execution/order_manager.py` — currently a stub with `_assert_paper_trading_disabled`. You will replace its contents but preserve that function.
6. `journal/outcome_collector.py` — see how the existing outcome logic reads from `yfinance_adapter` and computes triple-barrier labels. Your trade close detection will share this data path.
7. `config/arconian_config.yaml` — note `execution.paper_trading: true`, `risk.phase1_risk_per_trade: 0.005`, and all tp/stop params.

## The goal

Build a paper trading workflow that lets the operator see each scan's tradeable candidates, record a paper entry, and have outcomes tracked automatically. The whitepaper's §11 specifies: *"operator reviews the candidate, decides to take it or not, records a hypothetical fill at the signal price. Paper trade is logged: entry price, stop level, target level, position size (0.5% risk per §11). Outcome tracked: P&L computed when stop/target/time-stop triggers."*

**This is NOT a broker-simulator integration.** No Alpaca, no IBKR paper. Fills are recorded at operator-provided prices. Outcome tracking uses the EOD price data the system already collects.

## Scope — what to build

### 1. Paper trade state machine (`execution/order_manager.py`)

Replace the current stub. Implement a pure, broker-agnostic module that manages the full lifecycle of a paper trade. This is the keystone — every other piece depends on it.

Public API (these function signatures are the contract):

```python
def open_paper_trade(
    session_scope,
    signal_id: int,
    entry_price: float,
    entry_time: datetime,
    direction: str,                 # 'long' or 'short'
    account_equity: float,
    atr_20: float,
    catalyst_flag: bool,
    config,
) -> Trade:
    """
    Creates a Trade row linked to the signal_log row.
    Computes stop, target, and share count per whitepaper §4.2 and §4.6.
    Writes entry_price, stop_price, target_price, shares, atr_at_entry.
    Sets status='open'. Sets signal_log.was_traded=True and trade_id.
    Raises ValueError on invalid inputs (direction not in {long,short}, atr<=0, etc).
    Never calls Schwab. Never hits a network.
    """

def check_open_trades(
    session_scope,
    yf_adapter,
    as_of_date: date,
    config,
) -> dict:
    """
    For every Trade with status='open':
      1. Fetch EOD OHLC for every trading day since entry_time (use yf_adapter).
      2. Walk day by day checking in this order per whitepaper §4.6:
         a. Stop hit (low <= stop for long, high >= stop for short) → close at stop_price, exit_reason='stop'.
         b. Target hit (high >= target for long, low <= target for short) → close at target_price, exit_reason='target'.
         c. Time stop: trading-day count since entry_time > config.execution.time_stop_days → close at next session's open, exit_reason='time_stop'.
      3. Close behaviour: set exit_price/exit_time/exit_reason/status='closed',
         compute realized_pnl and realized_r_multiple, propagate to signal_log.
      4. If both stop and target possible on same day, record as 'stop' (pessimistic —
         we cannot tell intraday order from EOD data). Log this as an ambiguous_exit.
    Returns {processed, closed_stop, closed_target, closed_time, errors}.
    """

def close_paper_trade_manual(
    session_scope,
    trade_id: int,
    exit_price: float,
    exit_time: datetime,
    reason: str = 'manual',
) -> Trade:
    """Operator-initiated close. Writes exit fields and status='closed'."""
```

Sizing rules (implement exactly these, reference `arconian-v3.md` §4.2):
- `risk_per_trade = account_equity * config.risk.phase1_risk_per_trade` (0.5% by default, **not** base_risk_per_trade — Phase 1 uses phase1_risk_per_trade).
- `atr_multiplier = config.risk.atr_stop_multiplier * (config.risk.catalyst_atr_premium if catalyst_flag else 1.0)`.
- `stop_distance = atr_multiplier * atr_20`.
- `stop_price = entry_price - stop_distance` (long), `entry_price + stop_distance` (short).
- `target_price = entry_price + (config.execution.tp1_r_multiple * stop_distance)` (long, negate for short). **Simplification for Phase 1**: use a single target at TP1 R-multiple. The tiered exit (50% at TP1, trail remainder) is explicitly deferred — note this as a TODO in the docstring and do not implement the trailing stop until outcome tracking is working end-to-end.
- `shares = floor(risk_per_trade / stop_distance)`. Round to whole shares; do not use fractional shares in Phase 1.
- Hard cap: `shares * entry_price <= account_equity * config.risk.max_position_pct`. If violated, cap shares and log a `position_capped` flag.
- If `shares == 0` (ATR too large relative to risk budget), raise `ValueError("Position sized to zero shares — risk/ATR too small")`.

Exit accounting:
- `realized_pnl = (exit_price - entry_price) * shares` for long, negated for short. **Gross of costs** — Phase 1 paper trades do not model transaction costs at the Trade level; costs are applied at the reporting layer per whitepaper §5.
- `realized_r_multiple = (exit_price - entry_price) / stop_distance` for long; negate for short.
- Propagate `realized_pnl`, `realized_r_multiple`, `entry_price`, `exit_price` back to the linked `signal_log` row.

Tests required (under `tests/test_order_manager.py`):
- Long stop hit on day 2.
- Short target hit on day 1.
- Time stop hit at day+3 open.
- Same-day stop and target → recorded as stop.
- Catalyst flag widens stop (2.25× ATR vs 1.5× ATR, same risk budget → fewer shares).
- Zero-share sizing raises ValueError.
- Position cap triggers when ATR is very small relative to entry price.
- Paper trade opens, then `check_open_trades` leaves it open when no barrier is breached.
- Manual close writes correct fields.

Do **not** call `_assert_paper_trading_disabled` anywhere in this module — that guard is for Phase 2+ live order submission. Paper trades are the entire point of Phase 1.

### 2. CLI for daily candidate review (`scripts/phase1_daily_review.py`)

A simple interactive CLI that:
1. Queries `signal_log` for today's most recent scan's candidates that:
   - `composite_score >= 0.70` (configurable flag `--min-score`, default 0.70 per CONTEXT.md open question #3).
   - `direction_signal` in `{'bullish', 'bearish'}` (ambiguous excluded).
   - `earnings_proximity_tag != 'excluded'`.
   - `corporate_action_flag = False`.
   - `history_status = 'full'`.
   - `was_traded = False`.
2. For each candidate, prints: ticker, direction, composite score, signal component breakdown (vol/return/options/sector_rs/delay percentiles), catalyst_flag, market_cap, days_to_next_earnings, prior_day_vwap, atr_20, suggested entry (signal-time mid proxy — pull `Close` from yfinance for scan date as a stand-in for Phase 1; document that this is a simplification).
3. Prompts: `[t]ake  [s]kip  [q]uit` for each.
4. On `t`: prompts for entry price (default: the suggested entry), then calls `open_paper_trade` with `account_equity` from a new CLI arg `--equity` (required, no default).
5. Prints the resulting stop, target, and share count, and the signal_log id now linked.

Do not build a Streamlit dashboard. CLI only for the MVP — Zinniinae will grow the dashboard later.

### 3. Scheduled trade-state checker

Add a daily job to `execution/scan_scheduler.py` that calls `check_open_trades` at 4:30 PM ET (after the existing 4:15 PM outcome_collector maintenance block). This closes trades from the prior session's price action.

Use the existing `APScheduler` setup — do not add a new scheduler. Register with the same scheduler instance.

### 4. Telegram notification on fill events

The existing Telegram integration (see how `main.py` and `deadman_switch.py` use it) already handles scan alerts. Extend it to also send:
- When a paper trade is opened (via `open_paper_trade`): `"📝 Paper {long/short} {ticker} @ ${entry} | stop ${stop} | target ${target} | {shares} shares"`.
- When a paper trade closes (via `check_open_trades`): `"{✅/❌} Paper {ticker} closed: {exit_reason} @ ${exit} | R={r_multiple:.2f} | P&L ${realized_pnl}"`.

Do **not** touch the dead-man's switch or the startup/scan alert paths.

### 5. Parallel tracking fields on `Trade` (deferred — do not build in this PR)

The whitepaper §11 "Paper-to-Live Transition Framework" requires parallel paper/live tracking with fill_rate_drag, slippage_drag, latency_drag, adherence_drag, override_drag decomposition. **This is Phase 1 late-stage, not MVP.** Do not add columns to `Trade` yet. When the first 10 paper trades are logged we will revisit.

Similarly deferred: the tiered exit (50% at 2R, trail remainder), partial fill modeling, after-tax P&L computation (`realized_pnl_after_tax`). All are in the whitepaper but are beyond the MVP paper trader.

## Hard constraints

- **`paper_trading: true` stays true.** Do not touch it, do not add any code path that would submit a Schwab order.
- **No network I/O from `order_manager.py`.** The module reads the DB and is called by external drivers. It does not import `schwab_adapter` or `yfinance_adapter`. The yfinance calls for EOD prices happen in `check_open_trades` via an injected `yf_adapter` arg — same dependency-injection pattern `outcome_collector.run` uses.
- **Follow the existing module style.** Read 2–3 modules in `journal/` and `risk/` to match docstring format, logging, error handling, and test conventions. Tests use pytest with fixtures from `tests/fixtures/`.
- **`str_replace` the existing `order_manager.py` stub** — do not delete the file. Keep `_assert_paper_trading_disabled` at the bottom of the new file, unchanged, with a comment marking it as "reserved for Phase 2+".
- **Windows paths.** All file paths use `pathlib.Path`. The project root is `C:\Users\<user>\Documents\arconian`.

## Acceptance criteria

- `python -m pytest tests/test_order_manager.py` passes with 8+ tests covering the cases above.
- `python -m pytest` overall still passes 316+ tests (no regressions).
- `python main.py --dry-run` still runs clean.
- `python scripts/phase1_daily_review.py --equity 10000` produces the candidate list and the interactive prompt.
- Opening a paper trade and running `check_open_trades` with a `as_of_date` ≥ 4 days later against a mocked yf_adapter produces the expected closed state (verify by querying the `trades` table directly).

## Out of scope — do not build

- Streamlit UI, web UI, Zinniinae integration changes.
- Trailing stop / tiered exit.
- Transaction cost modeling at the trade level.
- Parallel paper/live tracking and expectancy decomposition.
- Any change to the signal engine, universe manager, or risk engine.
- Any change to `arconian_config.yaml` schema.
- Schwab order placement or any code that would send a real order.
