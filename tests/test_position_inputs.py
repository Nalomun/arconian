"""Tests for risk/position_inputs.py (#18 follow-on enrichment)."""

import sys
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from risk.position_inputs import (
    fetch_sub_industry,
    fetch_returns_60d,
    fetch_position_risk_inputs,
)


def _yf_with_closes(closes, ticker="X", multiindex=True):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="B")
    if multiindex:
        cols = pd.MultiIndex.from_tuples([("Close", ticker), ("Open", ticker)])
        df = pd.DataFrame(np.column_stack([closes, closes]), index=idx, columns=cols)
    else:
        df = pd.DataFrame({"Close": closes, "Open": closes}, index=idx)
    yf = MagicMock()
    yf.get_daily_prices.return_value = df
    return yf


class TestSubIndustry:
    def test_returns_industry(self):
        yf = MagicMock()
        yf.get_ticker_info.return_value = SimpleNamespace(industry="Biotechnology")
        assert fetch_sub_industry(yf, "ABC") == "Biotechnology"

    def test_none_when_info_missing(self):
        yf = MagicMock()
        yf.get_ticker_info.return_value = None
        assert fetch_sub_industry(yf, "ABC") is None

    def test_none_on_exception(self):
        yf = MagicMock()
        yf.get_ticker_info.side_effect = RuntimeError("boom")
        assert fetch_sub_industry(yf, "ABC") is None


class TestReturns60d:
    def test_computes_returns(self):
        closes = [100.0 * (1.01 ** i) for i in range(63)]
        yf = _yf_with_closes(closes, "X", multiindex=True)
        rets = fetch_returns_60d(yf, "X")
        assert rets is not None
        assert len(rets) == 60
        # Each step is ~+1%.
        assert rets[-1] == pytest.approx(0.01, rel=1e-6)

    def test_single_level_frame(self):
        closes = [10.0 + i for i in range(20)]
        yf = _yf_with_closes(closes, "X", multiindex=False)
        rets = fetch_returns_60d(yf, "X")
        assert rets is not None
        assert len(rets) == 19  # 20 closes → 19 returns

    def test_none_on_empty(self):
        yf = MagicMock()
        yf.get_daily_prices.return_value = None
        assert fetch_returns_60d(yf, "X") is None

    def test_none_on_too_few(self):
        yf = _yf_with_closes([10.0, 10.1], "X", multiindex=False)  # 1 return < min 4
        assert fetch_returns_60d(yf, "X") is None


class TestFetchPositionRiskInputs:
    def test_none_adapter_returns_none_pair(self):
        assert fetch_position_risk_inputs(None, "X") == (None, None)

    def test_combined(self):
        closes = [50.0 + i for i in range(30)]
        yf = _yf_with_closes(closes, "X", multiindex=False)
        yf.get_ticker_info.return_value = SimpleNamespace(industry="Software")
        sub, rets = fetch_position_risk_inputs(yf, "X")
        assert sub == "Software"
        assert rets is not None and len(rets) == 29
