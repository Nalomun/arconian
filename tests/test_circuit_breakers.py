"""Tests for risk/circuit_breakers.py — CB4 trading-day halt (F-17)."""

import sys
import os
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from db import init_db, reset_engine_for_testing, session_scope
from risk.circuit_breakers import (
    CircuitBreakerManager,
    CircuitBreakerState,
    _add_trading_days,
    load_cb_state,
    save_cb_state,
)


@pytest.fixture
def isolated_db():
    reset_engine_for_testing("sqlite:///:memory:")
    init_db()
    yield
    reset_engine_for_testing("sqlite:///:memory:")


class TestAddTradingDays:
    def test_friday_plus_two_is_tuesday(self):
        # 2026-06-19 is a Friday; +2 trading days = Tuesday 2026-06-23.
        assert _add_trading_days(date(2026, 6, 19), 2) == date(2026, 6, 23)

    def test_midweek(self):
        # Monday 2026-06-22 +2 = Wednesday 2026-06-24.
        assert _add_trading_days(date(2026, 6, 22), 2) == date(2026, 6, 24)


class TestCB4TradingDayHalt:
    def _trigger_cb4(self, trigger_day):
        cb = CircuitBreakerManager(config_max_positions=5)
        # A >3% single-day loss on the trigger day.
        cb.record_day(day_pnl=-5_000.0, equity=100_000.0, today=trigger_day)
        return cb

    def test_friday_loss_blocks_through_tuesday(self):
        """F-17: a Friday CB4 trigger must still block Mon and Tue, not expire
        over the weekend."""
        cb = self._trigger_cb4(date(2026, 6, 19))  # Friday
        monday = date(2026, 6, 22)
        tuesday = date(2026, 6, 23)
        wednesday = date(2026, 6, 24)

        assert cb.allow_new_positions(today=monday)[0] is False
        assert cb.allow_new_positions(today=tuesday)[0] is False
        # Resumes on Wednesday.
        assert cb.allow_new_positions(today=wednesday)[0] is True

    def test_no_trigger_when_loss_below_threshold(self):
        cb = CircuitBreakerManager(config_max_positions=5)
        cb.record_day(day_pnl=-1_000.0, equity=100_000.0, today=date(2026, 6, 19))
        assert cb.allow_new_positions(today=date(2026, 6, 22))[0] is True

    def test_gain_does_not_trigger(self):
        cb = CircuitBreakerManager(config_max_positions=5)
        cb.record_day(day_pnl=+5_000.0, equity=100_000.0, today=date(2026, 6, 19))
        assert cb.allow_new_positions(today=date(2026, 6, 22))[0] is True


class TestCBStatePersistence:
    def test_load_returns_none_when_empty(self, isolated_db):
        assert load_cb_state(session_scope) is None

    def test_save_then_load_roundtrip(self, isolated_db):
        state = CircuitBreakerState(
            consecutive_losses=4,
            cb1_trades_remaining=7,
            cb2_trigger_date=date(2026, 6, 10),
            cb3_halted=True,
            cb4_halt_until=date(2026, 6, 23),
            equity_log=[(date(2026, 6, 18), 100_000.0), (date(2026, 6, 19), 99_500.0)],
        )
        save_cb_state(session_scope, state)

        loaded = load_cb_state(session_scope)
        assert loaded.consecutive_losses == 4
        assert loaded.cb1_trades_remaining == 7
        assert loaded.cb2_trigger_date == date(2026, 6, 10)
        assert loaded.cb3_halted is True
        assert loaded.cb4_halt_until == date(2026, 6, 23)
        assert loaded.equity_log == [
            (date(2026, 6, 18), 100_000.0), (date(2026, 6, 19), 99_500.0),
        ]

    def test_save_is_idempotent_singleton(self, isolated_db):
        save_cb_state(session_scope, CircuitBreakerState(consecutive_losses=1))
        save_cb_state(session_scope, CircuitBreakerState(consecutive_losses=2))
        from models import CircuitBreakerStateRow
        with session_scope() as s:
            rows = s.query(CircuitBreakerStateRow).all()
        assert len(rows) == 1
        assert rows[0].consecutive_losses == 2

    def test_manager_load_and_persist_survive_restart(self, isolated_db):
        # Trip CB4 on a Friday, persist, then reload into a fresh manager.
        cb = CircuitBreakerManager(config_max_positions=5)
        cb.record_day(day_pnl=-5_000.0, equity=100_000.0, today=date(2026, 6, 19))
        cb.persist(session_scope)

        revived = CircuitBreakerManager.load(session_scope, config_max_positions=5)
        # The halt survived the "restart": Monday is still blocked.
        assert revived.allow_new_positions(today=date(2026, 6, 22))[0] is False

    def test_manager_load_fresh_when_no_state(self, isolated_db):
        cb = CircuitBreakerManager.load(session_scope, config_max_positions=5)
        assert cb.allow_new_positions(today=date(2026, 6, 22))[0] is True
