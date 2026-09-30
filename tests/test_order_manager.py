"""
Tests for execution/order_manager.py:
  - open_paper_trade: sizing, stop/target computation, signal_log linkage
  - check_open_trades: stop hit, target hit, time stop, ambiguous exit,
                       catalyst widens stop, no-barrier leave-open
  - close_paper_trade_manual: field writes, P&L computation
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, datetime, timedelta
from timeutils import utcnow
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from db import init_db, reset_engine_for_testing, session_scope
from models import SignalLog, Trade


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_db():
    reset_engine_for_testing("sqlite:///:memory:")
    init_db()
    yield
    reset_engine_for_testing("sqlite:///:memory:")


def _make_config(
    phase1_risk=0.005,
    atr_stop_mult=1.5,
    catalyst_premium=1.5,
    max_position_pct=0.10,
    tp1_r_multiple=2.0,
    tp1_exit_pct=0.50,
    trailing_stop_atr_multiple=1.0,
    time_stop_days=3,
):
    cfg = MagicMock()
    cfg.risk.phase1_risk_per_trade = phase1_risk
    cfg.risk.atr_stop_multiplier = atr_stop_mult
    cfg.risk.catalyst_atr_premium = catalyst_premium
    cfg.risk.max_position_pct = max_position_pct
    cfg.execution.tp1_r_multiple = tp1_r_multiple
    cfg.execution.tp1_exit_pct = tp1_exit_pct
    cfg.execution.trailing_stop_atr_multiple = trailing_stop_atr_multiple
    cfg.execution.time_stop_days = time_stop_days
    return cfg


def _seed_signal(ticker="AAPL", direction="bullish", atr_20=2.0, catalyst_flag=False) -> int:
    """Insert a minimal SignalLog row and return its id."""
    with session_scope() as session:
        row = SignalLog(
            scan_timestamp=utcnow(),
            scan_type="midday",
            ticker=ticker,
            composite_score=0.80,
            direction_signal=direction,
            atr_20=atr_20,
            history_status="full",
            corporate_action_flag=False,
            catalyst_flag=catalyst_flag,
            earnings_proximity_tag="normal",
        )
        session.add(row)
        session.flush()
        sid = row.id
    return sid


def _make_ohlc_df(rows: list[tuple]) -> pd.DataFrame:
    """
    Build a yfinance-style DataFrame from (date, open, high, low, close) tuples.
    """
    dates = pd.DatetimeIndex([pd.Timestamp(d) for d, *_ in rows])
    return pd.DataFrame(
        {
            "Open":  [o for _, o, h, l, c in rows],
            "High":  [h for _, o, h, l, c in rows],
            "Low":   [l for _, o, h, l, c in rows],
            "Close": [c for _, o, h, l, c in rows],
        },
        index=dates,
    )


def _make_yf(rows: list[tuple]) -> MagicMock:
    """Return a mock yf_adapter whose get_price_history returns the given rows."""
    yf = MagicMock()
    yf.get_price_history.return_value = _make_ohlc_df(rows)
    return yf


# ---------------------------------------------------------------------------
# open_paper_trade — sizing and DB writes
# ---------------------------------------------------------------------------

class TestOpenPaperTrade:

    def test_long_stop_hit_day2(self):
        """Verify open_paper_trade creates correct stop/target/shares for a long."""
        from execution.order_manager import open_paper_trade

        sid = _seed_signal(ticker="TSLA", atr_20=2.0)
        config = _make_config()

        # risk=50, stop_dist=3.0, shares=floor(50/3)=16; 16*50=800 ≤ 10000*0.10=1000 → no cap
        trade = open_paper_trade(
            session_scope=session_scope,
            signal_id=sid,
            entry_price=50.0,
            entry_time=datetime(2026, 1, 5, 10, 0),
            direction="long",
            account_equity=10_000.0,
            atr_20=2.0,
            catalyst_flag=False,
            config=config,
        )

        assert trade.status == "open"
        assert trade.direction == "long"
        assert abs(trade.stop_price - 47.0) < 1e-9    # 50 - 3
        assert abs(trade.target_price - 56.0) < 1e-9  # 50 + 2*3
        assert trade.shares == 16.0

        with session_scope() as session:
            sig = session.get(SignalLog, sid)
        assert sig.was_traded is True
        assert sig.trade_id == trade.id
        assert abs(sig.entry_price - 50.0) < 1e-9

    def test_short_open_sizes_correctly(self):
        """Short trade: stop above entry, target below entry."""
        from execution.order_manager import open_paper_trade

        sid = _seed_signal(ticker="AMZN", direction="bearish", atr_20=2.0)
        config = _make_config()

        # entry=50, stop_dist=3 → stop=53, target=44; 16*50=800 ≤ 1000 → no cap
        trade = open_paper_trade(
            session_scope=session_scope,
            signal_id=sid,
            entry_price=50.0,
            entry_time=datetime(2026, 1, 5, 10, 0),
            direction="short",
            account_equity=10_000.0,
            atr_20=2.0,
            catalyst_flag=False,
            config=config,
        )

        assert abs(trade.stop_price - 53.0) < 1e-9   # 50 + 3
        assert abs(trade.target_price - 44.0) < 1e-9  # 50 - 2*3
        assert trade.shares == 16.0

    def test_catalyst_flag_widens_stop_reduces_shares(self):
        """Catalyst premium raises ATR multiplier → wider stop → fewer shares."""
        from execution.order_manager import open_paper_trade

        sid_no_cat = _seed_signal(ticker="A", atr_20=2.0)
        sid_cat    = _seed_signal(ticker="B", atr_20=2.0, catalyst_flag=True)
        config = _make_config()

        # entry=50; no-cap: 16*50=800≤1000; cat: 11*50=550≤1000
        no_cat = open_paper_trade(
            session_scope=session_scope,
            signal_id=sid_no_cat,
            entry_price=50.0,
            entry_time=datetime(2026, 1, 5, 10, 0),
            direction="long",
            account_equity=10_000.0,
            atr_20=2.0,
            catalyst_flag=False,
            config=config,
        )
        cat = open_paper_trade(
            session_scope=session_scope,
            signal_id=sid_cat,
            entry_price=50.0,
            entry_time=datetime(2026, 1, 5, 10, 0),
            direction="long",
            account_equity=10_000.0,
            atr_20=2.0,
            catalyst_flag=True,
            config=config,
        )

        # no-cat: stop_dist=3.0, shares=16; cat: stop_dist=4.5, shares=11
        assert no_cat.shares > cat.shares
        assert abs(cat.stop_price - 45.5) < 1e-9   # 50 - 1.5*1.5*2 = 50 - 4.5

    def test_zero_share_sizing_raises(self):
        """ATR too large relative to risk budget → ValueError."""
        from execution.order_manager import open_paper_trade

        sid = _seed_signal(atr_20=2000.0)
        config = _make_config()

        with pytest.raises(ValueError, match="zero shares"):
            open_paper_trade(
                session_scope=session_scope,
                signal_id=sid,
                entry_price=100.0,
                entry_time=datetime(2026, 1, 5, 10, 0),
                direction="long",
                account_equity=10_000.0,
                atr_20=2000.0,
                catalyst_flag=False,
                config=config,
            )

    def test_position_cap_triggers(self):
        """Very small ATR → uncapped shares would exceed max_position_pct → capped."""
        from execution.order_manager import open_paper_trade

        sid = _seed_signal(atr_20=0.001)
        config = _make_config(max_position_pct=0.10)

        # risk=50, stop_dist=0.0015, uncapped=33333 shares * 100 = 3.3M >> 10000*0.10=1000
        # capped: floor(1000 / 100) = 10
        trade = open_paper_trade(
            session_scope=session_scope,
            signal_id=sid,
            entry_price=100.0,
            entry_time=datetime(2026, 1, 5, 10, 0),
            direction="long",
            account_equity=10_000.0,
            atr_20=0.001,
            catalyst_flag=False,
            config=config,
        )

        assert trade.shares == 10.0
        assert trade.shares * 100.0 <= 10_000.0 * 0.10 + 1.0  # within cap

    def test_invalid_direction_raises(self):
        from execution.order_manager import open_paper_trade

        sid = _seed_signal()
        config = _make_config()
        with pytest.raises(ValueError, match="direction"):
            open_paper_trade(
                session_scope, sid, 100.0, utcnow(),
                "sideways", 10_000.0, 2.0, False, config,
            )

    def test_nonexistent_signal_raises(self):
        from execution.order_manager import open_paper_trade

        config = _make_config()
        with pytest.raises(ValueError, match="not found"):
            open_paper_trade(
                session_scope, 99999, 100.0, utcnow(),
                "long", 10_000.0, 2.0, False, config,
            )


# ---------------------------------------------------------------------------
# check_open_trades — barrier detection
# ---------------------------------------------------------------------------

ENTRY_DATE = date(2026, 1, 5)   # Monday


def _open_trade(ticker="AAPL", direction="long", entry=100.0, atr=2.0, equity=10_000.0):
    """Open a paper trade and return (trade_id, signal_id)."""
    from execution.order_manager import open_paper_trade

    sid = _seed_signal(ticker=ticker, direction="bullish" if direction == "long" else "bearish", atr_20=atr)
    config = _make_config()
    trade = open_paper_trade(
        session_scope=session_scope,
        signal_id=sid,
        entry_price=entry,
        entry_time=datetime.combine(ENTRY_DATE, datetime.min.time()),
        direction=direction,
        account_equity=equity,
        atr_20=atr,
        catalyst_flag=False,
        config=config,
    )
    return trade.id, sid


class TestCheckOpenTrades:

    def test_long_stop_hit_on_day2(self):
        """Long trade: stop triggered on the 2nd trading day after entry."""
        from execution.order_manager import check_open_trades

        trade_id, _ = _open_trade(ticker="AAPL", direction="long", entry=100.0, atr=2.0)
        # stop=97, target=106
        rows = [
            (ENTRY_DATE,                     100.0, 102.0, 98.5, 101.0),  # entry day (skipped)
            (ENTRY_DATE + timedelta(days=1), 101.0, 101.5, 98.0, 101.0),  # day1: L=98 > 97 ✓
            (ENTRY_DATE + timedelta(days=2), 100.5, 98.0,  96.0, 97.5),   # day2: L=96 <= 97 → stop
        ]
        yf = _make_yf(rows)
        config = _make_config()
        as_of = ENTRY_DATE + timedelta(days=2)

        result = check_open_trades(session_scope, yf, as_of, config)

        assert result["closed_stop"] == 1
        with session_scope() as session:
            t = session.get(Trade, trade_id)
        assert t.status == "closed"
        assert t.exit_reason == "stop"
        assert abs(t.exit_price - 97.0) < 1e-9
        assert t.realized_pnl == (97.0 - 100.0) * t.shares  # negative

    def test_short_target_triggers_tp1_partial_exit(self):
        """Short trade: target hit triggers TP1 partial — trade stays open with trailing stop."""
        from execution.order_manager import check_open_trades

        trade_id, _ = _open_trade(ticker="MSFT", direction="short", entry=50.0, atr=2.0)
        # entry=50, stop=53, target=44; tp1_shares=8, remaining=8; trailing=46 (44+2*1)
        rows = [
            (ENTRY_DATE,                     50.0, 51.0, 49.0, 50.0),
            (ENTRY_DATE + timedelta(days=1), 49.0, 51.0, 43.0, 46.0),  # L=43 <= target=44 → TP1
        ]
        yf = _make_yf(rows)
        config = _make_config()

        result = check_open_trades(session_scope, yf, ENTRY_DATE + timedelta(days=1), config)

        assert result["tp1_partial"] == 1
        with session_scope() as session:
            t = session.get(Trade, trade_id)
        assert t.status == "open"
        assert t.tp1_hit is True
        assert t.shares_remaining == 8.0
        assert abs(t.trailing_stop_price - 46.0) < 1e-9  # 44 + 2*1

    def test_time_stop_closes_at_next_day_open(self):
        """No barrier hit in 3 days → time stop, closed at day+4 open."""
        from execution.order_manager import check_open_trades

        trade_id, _ = _open_trade(ticker="GOOG", direction="long", entry=100.0, atr=2.0)
        # stop=97, target=106
        d1 = ENTRY_DATE + timedelta(days=1)
        d2 = ENTRY_DATE + timedelta(days=2)
        d3 = ENTRY_DATE + timedelta(days=3)
        d4 = ENTRY_DATE + timedelta(days=4)
        rows = [
            (ENTRY_DATE, 100.0, 101.0, 99.0, 100.5),  # entry day
            (d1, 100.5, 101.5, 98.0, 101.0),           # day1: safe
            (d2, 101.0, 102.0, 98.5, 101.5),           # day2: safe
            (d3, 101.5, 102.5, 98.5, 102.0),           # day3: safe → time stop triggers
            (d4, 101.8, 103.0, 100.0, 102.5),          # day4: provides open for exit
        ]
        yf = _make_yf(rows)
        config = _make_config(time_stop_days=3)

        result = check_open_trades(session_scope, yf, d4, config)

        assert result["closed_time"] == 1
        with session_scope() as session:
            t = session.get(Trade, trade_id)
        assert t.status == "closed"
        assert t.exit_reason == "time_stop"
        # Exit at day4's open = 101.8
        assert abs(t.exit_price - 101.8) < 1e-9

    def test_same_day_stop_and_target_recorded_as_stop(self):
        """Both stop and target breached on the same day → pessimistic → stop."""
        from execution.order_manager import check_open_trades

        trade_id, _ = _open_trade(ticker="META", direction="long", entry=100.0, atr=2.0)
        # stop=97, target=106
        rows = [
            (ENTRY_DATE,                     100.0, 101.0, 99.0, 100.0),
            (ENTRY_DATE + timedelta(days=1), 100.0, 110.0, 94.0, 100.0),  # H > 106 AND L < 97
        ]
        yf = _make_yf(rows)
        config = _make_config()

        result = check_open_trades(session_scope, yf, ENTRY_DATE + timedelta(days=1), config)

        assert result["closed_stop"] == 1
        with session_scope() as session:
            t = session.get(Trade, trade_id)
        assert t.exit_reason == "stop"
        assert abs(t.exit_price - 97.0) < 1e-9

    def test_trade_left_open_when_no_barrier_breached(self):
        """If price stays between stop and target, trade remains open."""
        from execution.order_manager import check_open_trades

        trade_id, _ = _open_trade(ticker="NVDA", direction="long", entry=100.0, atr=2.0)
        # stop=97, target=106
        rows = [
            (ENTRY_DATE,                     100.0, 101.0, 99.0, 100.5),
            (ENTRY_DATE + timedelta(days=1), 100.5, 102.0, 98.0, 101.0),  # safe
        ]
        yf = _make_yf(rows)
        config = _make_config()

        result = check_open_trades(session_scope, yf, ENTRY_DATE + timedelta(days=1), config)

        assert result["closed_stop"] == 0
        assert result["closed_trailing"] == 0
        assert result["closed_time"] == 0
        with session_scope() as session:
            t = session.get(Trade, trade_id)
        assert t.status == "open"

    def test_trailing_stop_closes_after_tp1(self):
        """Long: TP1 on day1; ratchet on day2 lifts trailing stop; day3 trailing hit."""
        from execution.order_manager import check_open_trades

        trade_id, _ = _open_trade(ticker="NVDA", direction="long", entry=50.0, atr=2.0)
        # entry=50, stop=47, target=56; tp1_shares=8, remaining=8; initial trailing=54 (56-2*1)
        rows = [
            (ENTRY_DATE,                     50.0, 51.0, 49.5, 50.0),
            (ENTRY_DATE + timedelta(days=1), 55.0, 58.0, 52.0, 58.0),  # H=58>=56 → TP1; trailing=54
            (ENTRY_DATE + timedelta(days=2), 57.0, 59.0, 55.5, 58.0),  # else: L>54→safe; close=58→ratchet trailing to 56
            (ENTRY_DATE + timedelta(days=3), 56.5, 57.5, 55.5, 56.5),  # else: L=55.5<=trailing=56 → close at 56
        ]
        yf = _make_yf(rows)
        config = _make_config()

        result = check_open_trades(session_scope, yf, ENTRY_DATE + timedelta(days=3), config)

        assert result["closed_trailing"] == 1
        with session_scope() as session:
            t = session.get(Trade, trade_id)
        assert t.status == "closed"
        assert t.exit_reason == "trailing_stop"
        assert abs(t.exit_price - 56.0) < 1e-9
        # TP1: (56-50)*8=48; trailing: (56-50)*8=48; total=96
        assert abs(t.realized_pnl - 96.0) < 1e-6

    def test_time_stop_after_tp1(self):
        """TP1 on day1, no trailing stop hit in 3 days → time stop on remaining."""
        from execution.order_manager import check_open_trades

        trade_id, _ = _open_trade(ticker="META", direction="long", entry=100.0, atr=2.0)
        # stop=97, target=106; trailing=104 after TP1; time_stop_days=3
        d1 = ENTRY_DATE + timedelta(days=1)
        d2 = ENTRY_DATE + timedelta(days=2)
        d3 = ENTRY_DATE + timedelta(days=3)
        d4 = ENTRY_DATE + timedelta(days=4)
        rows = [
            (ENTRY_DATE, 100.0, 101.0, 99.0,  100.5),
            (d1,         105.0, 109.0, 99.0,  108.0),  # H>=106 → TP1; trailing=104→106 (from close=108)
            (d2,         107.5, 108.5, 105.5, 107.0),  # L=105.5 > trailing=106 → safe; trailing stays 106
            (d3,         106.5, 107.5, 106.0, 106.5),  # L=106.0 >= trailing=106 → safe; days_held=3 → time stop
            (d4,         106.0, 107.0, 105.0, 106.0),  # provides open for time stop exit
        ]
        yf = _make_yf(rows)
        config = _make_config(time_stop_days=3)

        result = check_open_trades(session_scope, yf, d4, config)

        assert result["closed_time"] == 1
        with session_scope() as session:
            t = session.get(Trade, trade_id)
        assert t.status == "closed"
        assert t.exit_reason == "time_stop"
        assert abs(t.exit_price - 106.0) < 1e-9  # d4 open

    def test_pnl_propagated_to_signal_log_after_full_close(self):
        """P&L propagated to signal_log only after the trailing leg fully closes."""
        from execution.order_manager import check_open_trades

        trade_id, sid = _open_trade(ticker="AMD", direction="long", entry=50.0, atr=2.0)
        # entry=50, stop=47, target=56; initial trailing=54 (56-2*1)
        rows = [
            (ENTRY_DATE,                     50.0, 51.0, 49.0,  50.5),
            (ENTRY_DATE + timedelta(days=1), 55.0, 58.0, 52.0,  57.0),  # H=58>=56 → TP1; trailing=54
            (ENTRY_DATE + timedelta(days=2), 53.5, 55.0, 53.0, 53.5),   # L=53.0 <= trailing=54 → close at 54
        ]
        yf = _make_yf(rows)
        config = _make_config()

        check_open_trades(session_scope, yf, ENTRY_DATE + timedelta(days=2), config)

        with session_scope() as session:
            sig = session.get(SignalLog, sid)
        assert sig.exit_price is not None
        assert sig.realized_pnl is not None
        # TP1: (56-50)*8=48; trailing: (54-50)*8=32; total=80
        assert abs(sig.realized_pnl - 80.0) < 1e-6


class TestEntryDayEvaluation:
    """F-19: the entry-day bar is now evaluated, pessimistically."""

    def test_entry_day_stop_closes_same_day(self):
        from execution.order_manager import check_open_trades
        trade_id, _ = _open_trade(ticker="EDX", direction="long", entry=100.0, atr=2.0)
        # stop=97. Entry-day low pierces it — previously ignored until the next bar.
        rows = [
            (ENTRY_DATE, 100.0, 101.0, 96.0, 99.0),  # L=96 <= stop 97 → stop on entry day
        ]
        result = check_open_trades(session_scope, _make_yf(rows), ENTRY_DATE, _make_config())
        assert result["closed_stop"] == 1
        with session_scope() as session:
            t = session.get(Trade, trade_id)
        assert t.exit_reason == "stop"
        assert abs(t.exit_price - 97.0) < 1e-9
        assert t.exit_time.date() == ENTRY_DATE

    def test_entry_day_short_stop_closes_same_day(self):
        from execution.order_manager import check_open_trades
        trade_id, _ = _open_trade(ticker="EDS", direction="short", entry=50.0, atr=2.0)
        # stop=53. Entry-day high pierces it.
        rows = [
            (ENTRY_DATE, 50.0, 54.0, 49.0, 50.0),  # H=54 >= stop 53 → stop
        ]
        result = check_open_trades(session_scope, _make_yf(rows), ENTRY_DATE, _make_config())
        assert result["closed_stop"] == 1
        with session_scope() as session:
            t = session.get(Trade, trade_id)
        assert abs(t.exit_price - 53.0) < 1e-9

    def test_entry_day_favorable_move_does_not_trigger_target(self):
        """Pessimistic: a favorable entry-day spike (possibly pre-entry) must NOT
        count as a target/TP1 hit."""
        from execution.order_manager import check_open_trades
        trade_id, _ = _open_trade(ticker="EDT", direction="long", entry=100.0, atr=2.0)
        # target=106; entry-day high blows past it but no stop breach.
        rows = [
            (ENTRY_DATE, 100.0, 110.0, 99.0, 101.0),  # H=110 >= target 106, L=99 > stop 97
        ]
        result = check_open_trades(session_scope, _make_yf(rows), ENTRY_DATE, _make_config())
        assert result["closed_stop"] == 0
        assert result.get("tp1_partial", 0) == 0
        with session_scope() as session:
            t = session.get(Trade, trade_id)
        assert t.status == "open"
        assert t.tp1_hit is False

    def test_entry_day_safe_then_next_day_stop(self):
        """Entry day within range; stop on the following day still works."""
        from execution.order_manager import check_open_trades
        trade_id, _ = _open_trade(ticker="EDN", direction="long", entry=100.0, atr=2.0)
        rows = [
            (ENTRY_DATE,                     100.0, 101.0, 98.5, 100.0),  # safe (L=98.5>97)
            (ENTRY_DATE + timedelta(days=1), 100.0, 100.5, 96.0, 97.0),  # L=96 → stop
        ]
        result = check_open_trades(
            session_scope, _make_yf(rows), ENTRY_DATE + timedelta(days=1), _make_config()
        )
        assert result["closed_stop"] == 1
        with session_scope() as session:
            t = session.get(Trade, trade_id)
        assert t.exit_time.date() == ENTRY_DATE + timedelta(days=1)


# ---------------------------------------------------------------------------
# close_paper_trade_manual
# ---------------------------------------------------------------------------

class TestClosePaperTradeManual:

    def test_manual_close_writes_correct_fields(self):
        from execution.order_manager import close_paper_trade_manual

        # entry=50 → shares=16 (16*50=800≤1000, no cap); stop_dist=3.0
        trade_id, sid = _open_trade(ticker="AAPL", direction="long", entry=50.0, atr=2.0)
        exit_dt = datetime(2026, 1, 7, 15, 30)

        result = close_paper_trade_manual(
            session_scope=session_scope,
            trade_id=trade_id,
            exit_price=52.0,
            exit_time=exit_dt,
            reason="manual",
        )

        assert result.status == "closed"
        assert result.exit_reason == "manual"
        assert abs(result.exit_price - 52.0) < 1e-9
        assert result.exit_time == exit_dt

        # shares=16, pnl = (52-50)*16 = 32
        assert abs(result.realized_pnl - 32.0) < 1e-9

        # R = (52-50)/3 = 0.667
        assert abs(result.realized_r_multiple - (2.0 / 3.0)) < 1e-3

    def test_manual_close_propagates_to_signal_log(self):
        from execution.order_manager import close_paper_trade_manual

        trade_id, sid = _open_trade(ticker="TSLA", direction="long", entry=100.0, atr=2.0)

        close_paper_trade_manual(session_scope, trade_id, 103.0, utcnow())

        with session_scope() as session:
            sig = session.get(SignalLog, sid)
        assert abs(sig.exit_price - 103.0) < 1e-9
        assert sig.realized_pnl is not None

    def test_manual_close_nonexistent_trade_raises(self):
        from execution.order_manager import close_paper_trade_manual

        with pytest.raises(ValueError, match="not found"):
            close_paper_trade_manual(session_scope, 99999, 100.0, utcnow())

    def test_manual_close_already_closed_raises(self):
        from execution.order_manager import close_paper_trade_manual

        trade_id, _ = _open_trade(ticker="AAPL")
        close_paper_trade_manual(session_scope, trade_id, 101.0, utcnow())

        with pytest.raises(ValueError, match="not open"):
            close_paper_trade_manual(session_scope, trade_id, 102.0, utcnow())


# ---------------------------------------------------------------------------
# _fetch_entry_price two-stage fallback
# ---------------------------------------------------------------------------

class TestFetchEntryPriceFallback:
    """
    Regression tests for the entry-price resolver. The pre-fix version used
    only fast_info, which silently returned None for thinly-traded small
    caps off-hours and dropped 100% of qualifying candidates on 2026-04-22.
    """

    def _stub_yf(self, live_price=None, live_raises=False, history_close=None,
                 history_raises=False):
        yf = MagicMock()
        if live_raises:
            yf.get_latest_price.side_effect = RuntimeError("boom")
        else:
            yf.get_latest_price.return_value = live_price

        if history_raises:
            yf.get_price_history.side_effect = RuntimeError("history boom")
        elif history_close is None:
            yf.get_price_history.return_value = None
        else:
            yf.get_price_history.return_value = pd.DataFrame(
                {"Close": [history_close]},
                index=pd.to_datetime(["2026-04-22"]),
            )
        return yf

    def test_uses_live_when_available(self):
        from execution.order_manager import _fetch_entry_price
        yf = self._stub_yf(live_price=42.5, history_close=99.0)
        assert _fetch_entry_price(yf, "AAPL") == 42.5
        yf.get_price_history.assert_not_called()

    def test_falls_back_when_live_returns_none(self):
        from execution.order_manager import _fetch_entry_price
        yf = self._stub_yf(live_price=None, history_close=37.25)
        assert _fetch_entry_price(yf, "JBIO") == 37.25
        yf.get_price_history.assert_called_once()

    def test_falls_back_when_live_raises(self):
        from execution.order_manager import _fetch_entry_price
        yf = self._stub_yf(live_raises=True, history_close=11.0)
        assert _fetch_entry_price(yf, "SPRY") == 11.0

    def test_skips_zero_live_price(self):
        from execution.order_manager import _fetch_entry_price
        # fast_info occasionally returns 0.0 for halted/illiquid names
        yf = self._stub_yf(live_price=0.0, history_close=8.5)
        assert _fetch_entry_price(yf, "FWRG") == 8.5

    def test_returns_none_when_both_paths_fail(self):
        from execution.order_manager import _fetch_entry_price
        yf = self._stub_yf(live_price=None, history_raises=True)
        assert _fetch_entry_price(yf, "GHOST") is None

    def test_returns_none_when_history_empty(self):
        from execution.order_manager import _fetch_entry_price
        yf = self._stub_yf(live_price=None, history_close=None)
        assert _fetch_entry_price(yf, "GHOST") is None

    def test_history_fallback_handles_multiindex_columns(self):
        # F-7: yfinance 1.3 returns MultiIndex columns even for one ticker, and
        # pandas 3 removed float(Series). The fallback must still yield a scalar.
        from execution.order_manager import _fetch_entry_price
        idx = pd.DatetimeIndex([pd.Timestamp("2025-01-13"), pd.Timestamp("2025-01-14")])
        cols = pd.MultiIndex.from_tuples(
            [("Open", "ABC"), ("High", "ABC"), ("Low", "ABC"), ("Close", "ABC")]
        )
        df = pd.DataFrame([[9.0, 9.5, 8.8, 9.1], [9.1, 9.4, 9.0, 9.3]], index=idx, columns=cols)
        yf = MagicMock()
        yf.get_latest_price.return_value = None
        yf.get_price_history.return_value = df
        assert _fetch_entry_price(yf, "ABC") == pytest.approx(9.3)


# ---------------------------------------------------------------------------
# auto_paper_trade_candidates — gating: off-hours, ticker dedup, concurrency cap
# ---------------------------------------------------------------------------

def _auto_config(max_concurrent=5, min_score=0.70, equity=26_000.0):
    cfg = _make_config()
    cfg.execution.auto_paper_trade_min_score = min_score
    cfg.execution.account_equity = equity
    cfg.risk.max_concurrent_positions = max_concurrent
    return cfg


_RTH = datetime(2025, 1, 15, 12, 0)   # Wed noon ET — regular trading hours
_SINCE = datetime(2000, 1, 1)


def _auto_yf(price=100.0):
    yf = MagicMock()
    yf.get_latest_price.return_value = price
    return yf


class TestAutoPaperTradeGating:

    def test_off_hours_batch_skipped(self):
        # Decision #5: a scan running after hours opens nothing (stale fills).
        from execution.order_manager import auto_paper_trade_candidates
        _seed_signal(ticker="AAA")
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, _auto_config(), _auto_yf(),
            now_et=datetime(2025, 1, 15, 22, 0),   # 10pm ET
        )
        assert res["opened"] == 0
        assert res.get("off_hours") is True

    def test_weekend_batch_skipped(self):
        from execution.order_manager import auto_paper_trade_candidates
        _seed_signal(ticker="AAA")
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, _auto_config(), _auto_yf(),
            now_et=datetime(2025, 1, 18, 12, 0),   # Saturday noon
        )
        assert res["opened"] == 0
        assert res.get("off_hours") is True

    def test_ticker_dedup_within_batch(self):
        # Two qualifying signals for the same ticker → exactly one trade opens.
        from execution.order_manager import auto_paper_trade_candidates
        _seed_signal(ticker="DUP")
        _seed_signal(ticker="DUP")
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, _auto_config(), _auto_yf(), now_et=_RTH,
        )
        assert res["opened"] == 1
        assert res["skipped"] >= 1

    def test_dedup_against_existing_open_trade(self):
        from execution.order_manager import auto_paper_trade_candidates, open_paper_trade
        cfg = _auto_config()
        sid = _seed_signal(ticker="HELD")
        open_paper_trade(
            session_scope=session_scope, signal_id=sid, entry_price=100.0,
            entry_time=utcnow(), direction="long",
            account_equity=26_000.0, atr_20=2.0, catalyst_flag=False, config=cfg,
        )
        _seed_signal(ticker="HELD")   # fresh untraded signal, same ticker
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, cfg, _auto_yf(), now_et=_RTH,
        )
        assert res["opened"] == 0

    def test_concurrency_cap_limits_opens(self):
        from execution.order_manager import auto_paper_trade_candidates
        for t in ["AA", "BB", "CC", "DD", "EE", "FF", "GG"]:
            _seed_signal(ticker=t)
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, _auto_config(max_concurrent=3), _auto_yf(), now_et=_RTH,
        )
        assert res["opened"] == 3
        assert res["capped"] >= 1

    def test_open_when_in_hours_under_cap(self):
        from execution.order_manager import auto_paper_trade_candidates
        _seed_signal(ticker="SOLO")
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, _auto_config(), _auto_yf(), now_et=_RTH,
        )
        assert res["opened"] == 1

    def test_early_close_afternoon_batch_skipped(self):
        # F-26: a 15:30 scan on a half-day (Black Friday 2026) must not open
        # paper trades at stale post-1pm-close prices.
        from execution.order_manager import auto_paper_trade_candidates
        _seed_signal(ticker="HALF")
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, _auto_config(), _auto_yf(),
            now_et=datetime(2026, 11, 27, 15, 30),  # Black Friday, after 1pm close
        )
        assert res["opened"] == 0
        assert res.get("off_hours") is True

    def test_early_close_morning_batch_opens(self):
        from execution.order_manager import auto_paper_trade_candidates
        _seed_signal(ticker="HALFAM")
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, _auto_config(), _auto_yf(),
            now_et=datetime(2026, 11, 27, 11, 0),  # before the 1pm half-day close
        )
        assert res["opened"] == 1


# ---------------------------------------------------------------------------
# auto_paper_trade_candidates — full risk-engine modes (#18d)
# ---------------------------------------------------------------------------

class _StubEngine:
    """Stand-in for RiskEngine.approve_new_position (engine internals are tested
    in test_risk_engine.py; here we only exercise the order_manager wiring)."""

    def __init__(self, approved, spec=None, reason="ok"):
        self._approved = approved
        self._spec = spec
        self._reason = reason
        self.calls = []

    def approve_new_position(self, **kwargs):
        self.calls.append(kwargs)
        return self._approved, self._spec, self._reason


def _spec(shares=7, stop_price=97.0, target_price_1=106.0):
    return SimpleNamespace(shares=shares, stop_price=stop_price, target_price_1=target_price_1)


class TestAutoPaperTradeFullEngine:

    def test_off_mode_does_not_call_engine(self):
        from execution.order_manager import auto_paper_trade_candidates
        cfg = _auto_config()
        cfg.risk.full_engine_mode = "off"
        _seed_signal(ticker="OFF")
        engine = _StubEngine(approved=False)
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, cfg, _auto_yf(), now_et=_RTH, risk_engine=engine,
        )
        assert res["opened"] == 1
        assert res["mode"] == "off"
        assert engine.calls == []

    def test_shadow_logs_reject_but_still_opens(self):
        from execution.order_manager import auto_paper_trade_candidates
        cfg = _auto_config()
        cfg.risk.full_engine_mode = "shadow"
        _seed_signal(ticker="SHDW")
        engine = _StubEngine(approved=False, reason="regime_crisis")
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, cfg, _auto_yf(), now_et=_RTH, risk_engine=engine,
        )
        # Shadow never changes behaviour: the trade still opens.
        assert res["opened"] == 1
        assert res["mode"] == "shadow"
        assert res["engine_rejected"] == 1
        assert len(engine.calls) == 1

    def test_enforce_reject_blocks_open(self):
        from execution.order_manager import auto_paper_trade_candidates
        cfg = _auto_config()
        cfg.risk.full_engine_mode = "enforce"
        _seed_signal(ticker="NOPE")
        engine = _StubEngine(approved=False, reason="overnight_exposure")
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, cfg, _auto_yf(), now_et=_RTH, risk_engine=engine,
        )
        assert res["opened"] == 0
        assert res["engine_rejected"] == 1

    def test_enforce_approve_opens_with_engine_sizing(self):
        from execution.order_manager import auto_paper_trade_candidates
        from models import Trade
        cfg = _auto_config()
        cfg.risk.full_engine_mode = "enforce"
        _seed_signal(ticker="YES")
        engine = _StubEngine(approved=True, spec=_spec(shares=7, stop_price=97.0,
                                                       target_price_1=106.0))
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, cfg, _auto_yf(price=100.0), now_et=_RTH,
            risk_engine=engine,
        )
        assert res["opened"] == 1
        with session_scope() as s:
            trade = s.query(Trade).filter(Trade.ticker == "YES").one()
        # Opened with the engine's spec sizing, not the local _compute_sizing.
        assert trade.shares == 7
        assert trade.stop_price == pytest.approx(97.0)
        assert trade.target_price == pytest.approx(106.0)

    def test_enriched_inputs_reach_engine(self):
        """#18 follow-on: sub_industry + returns_60d are fetched and passed to
        approve_new_position (not left None)."""
        from execution.order_manager import auto_paper_trade_candidates
        cfg = _auto_config()
        cfg.risk.full_engine_mode = "shadow"
        _seed_signal(ticker="ENR")

        # yf that supplies a price latest, an industry, and 60d of closes.
        idx = pd.date_range("2026-01-01", periods=63, freq="B")
        closes = [100.0 + i for i in range(63)]
        yf = MagicMock()
        yf.get_latest_price.return_value = 100.0
        yf.get_ticker_info.return_value = SimpleNamespace(industry="Semiconductors")
        yf.get_daily_prices.return_value = pd.DataFrame(
            {"Close": closes, "Open": closes}, index=idx
        )

        engine = _StubEngine(approved=True, spec=_spec())
        auto_paper_trade_candidates(
            session_scope, _SINCE, cfg, yf, now_et=_RTH, risk_engine=engine,
        )
        assert len(engine.calls) == 1
        kw = engine.calls[0]
        assert kw["sub_industry"] == "Semiconductors"
        assert kw["returns_60d"] is not None and len(kw["returns_60d"]) == 60

    def test_mode_set_but_no_engine_falls_back_to_off(self):
        from execution.order_manager import auto_paper_trade_candidates
        cfg = _auto_config()
        cfg.risk.full_engine_mode = "enforce"
        _seed_signal(ticker="FALLBACK")
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, cfg, _auto_yf(), now_et=_RTH, risk_engine=None,
        )
        # No engine supplied → minimal-cap path, trade opens.
        assert res["opened"] == 1
        assert res["mode"] == "off"


# ---------------------------------------------------------------------------
# feed_circuit_breakers_after_close (#18e)
# ---------------------------------------------------------------------------

def _seed_closed_trade(ticker, pnl, exit_time, direction="long"):
    with session_scope() as session:
        session.add(Trade(
            ticker=ticker, direction=direction, status="closed",
            entry_price=100.0, shares=10.0, stop_price=98.0, target_price=104.0,
            atr_at_entry=1.0, exit_price=100.0 + pnl / 10.0,
            exit_time=exit_time, exit_reason="stop", realized_pnl=pnl,
        ))


class TestFeedCircuitBreakers:

    def test_consecutive_losses_trip_cb1(self):
        from execution.order_manager import feed_circuit_breakers_after_close
        from risk.circuit_breakers import CircuitBreakerManager

        base = datetime(2026, 6, 19, 16, 30)
        for i in range(5):
            _seed_closed_trade(f"L{i}", pnl=-100.0, exit_time=base + timedelta(minutes=i))

        cb = CircuitBreakerManager(config_max_positions=5)
        feed_circuit_breakers_after_close(
            session_scope, cb, account_equity=100_000.0,
            since=datetime(2026, 6, 19, 0, 0), today=date(2026, 6, 19),
        )
        # 5 consecutive losses → CB1 reduced-risk mode.
        assert cb.get_risk_multiplier() == 0.5

    def test_big_day_loss_trips_cb4_and_persists(self):
        from execution.order_manager import feed_circuit_breakers_after_close
        from risk.circuit_breakers import CircuitBreakerManager, load_cb_state

        # One -$5,000 close today on ~$100k equity = 5% single-day loss > 3%.
        _seed_closed_trade("BIG", pnl=-5_000.0, exit_time=datetime(2026, 6, 19, 16, 30))

        cb = CircuitBreakerManager(config_max_positions=5)
        feed_circuit_breakers_after_close(
            session_scope, cb, account_equity=100_000.0,
            since=datetime(2026, 6, 19, 0, 0), today=date(2026, 6, 19),  # Friday
        )
        # CB4 halt blocks the next trading days...
        assert cb.allow_new_positions(today=date(2026, 6, 22))[0] is False
        # ...and the state was persisted (survives a reload).
        revived = CircuitBreakerManager.load(session_scope, config_max_positions=5)
        assert revived.allow_new_positions(today=date(2026, 6, 22))[0] is False

    def test_only_counts_trades_closed_since(self):
        from execution.order_manager import feed_circuit_breakers_after_close
        from risk.circuit_breakers import CircuitBreakerManager

        # An old loss before `since` must not advance today's streak.
        _seed_closed_trade("OLD", pnl=-100.0, exit_time=datetime(2026, 6, 1, 16, 30))
        cb = CircuitBreakerManager(config_max_positions=5)
        res = feed_circuit_breakers_after_close(
            session_scope, cb, account_equity=100_000.0,
            since=datetime(2026, 6, 19, 0, 0), today=date(2026, 6, 19),
        )
        assert res["closed_today"] == 0
