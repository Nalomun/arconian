"""
End-to-end integration: the REAL RiskEngine driven through
auto_paper_trade_candidates with the real config (#18).

order_manager tests use a stub engine and RiskEngine is unit-tested separately;
this closes the gap by running the actual approve_new_position path — real
config thresholds, regime, circuit breakers, enriched sub_industry/returns — end
to end against an in-memory DB. No network (yfinance is mocked), no live DB.
"""

import sys
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import pytest

from db import init_db, reset_engine_for_testing, session_scope
from models import SignalLog, Trade

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "arconian_config.yaml")
_RTH = datetime(2025, 1, 15, 12, 0)   # Wed noon ET — regular trading hours
_SINCE = datetime(2000, 1, 1)


@pytest.fixture(autouse=True)
def isolated_db():
    reset_engine_for_testing("sqlite:///:memory:")
    init_db()
    yield
    reset_engine_for_testing("sqlite:///:memory:")


def _seed(ticker, score=0.80, direction="bullish", sector_etf="XLK", atr=2.0):
    with session_scope() as session:
        session.add(SignalLog(
            scan_timestamp=datetime(2025, 1, 15, 11, 0),
            scan_type="midday", ticker=ticker, composite_score=score,
            direction_signal=direction, atr_20=atr, history_status="full",
            corporate_action_flag=False, catalyst_flag=False,
            earnings_proximity_tag="normal", sector_etf=sector_etf,
        ))


def _yf():
    idx = pd.date_range("2024-10-01", periods=63, freq="B")
    closes = [100.0 + i * 0.1 for i in range(63)]
    yf = MagicMock()
    yf.get_latest_price.return_value = 100.0
    yf.get_ticker_info.return_value = SimpleNamespace(industry="Semiconductors")
    yf.get_daily_prices.return_value = pd.DataFrame(
        {"Close": closes, "Open": closes}, index=idx
    )
    return yf


def _engine(mode):
    from config.config_loader import ConfigLoader
    from risk.circuit_breakers import CircuitBreakerManager
    from risk.risk_engine import RiskEngine

    cfg = ConfigLoader(_CONFIG_PATH)
    cfg.risk.full_engine_mode = mode
    cb = CircuitBreakerManager(config_max_positions=cfg.risk.max_concurrent_positions)
    return cfg, RiskEngine(config=cfg, cb_manager=cb)


class TestRealEngineEndToEnd:

    def test_shadow_opens_and_logs_decision(self):
        from execution.order_manager import auto_paper_trade_candidates
        from risk.risk_engine import RegimeLevel

        cfg, engine = _engine("shadow")
        _seed("NVDA")
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, cfg, _yf(), now_et=_RTH,
            risk_engine=engine, regime=RegimeLevel.NORMAL,
        )
        assert res["mode"] == "shadow"
        # Shadow never blocks → the trade opens via the normal path.
        assert res["opened"] == 1

    def test_enforce_crisis_blocks_all(self):
        from execution.order_manager import auto_paper_trade_candidates
        from risk.risk_engine import RegimeLevel

        cfg, engine = _engine("enforce")
        _seed("NVDA")
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, cfg, _yf(), now_et=_RTH,
            risk_engine=engine, regime=RegimeLevel.CRISIS,
        )
        # Crisis regime → engine rejects → nothing opens.
        assert res["opened"] == 0
        assert res["engine_rejected"] == 1

    def test_enforce_normal_opens_with_engine_sizing(self):
        from execution.order_manager import auto_paper_trade_candidates
        from risk.risk_engine import RegimeLevel

        cfg, engine = _engine("enforce")
        _seed("NVDA")
        res = auto_paper_trade_candidates(
            session_scope, _SINCE, cfg, _yf(), now_et=_RTH,
            risk_engine=engine, regime=RegimeLevel.NORMAL,
        )
        assert res["opened"] == 1
        with session_scope() as s:
            trade = s.query(Trade).filter(Trade.ticker == "NVDA").one()
        # Sizing came from the engine spec: max_position_pct cap = 0.10*26000/100 = 26.
        assert trade.shares == 26
