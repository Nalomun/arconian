"""Tests for risk/regime_inputs.py (Tier-3 #18)."""

import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from risk.regime_inputs import fetch_regime_inputs, _close_series


def _multiindex_frame(vix_closes, iwm_closes):
    """Build a yfinance-style MultiIndex frame with Close for ^VIX and IWM."""
    n = max(len(vix_closes), len(iwm_closes))
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    cols = pd.MultiIndex.from_tuples(
        [("Close", "^VIX"), ("Close", "IWM")]
    )
    data = np.column_stack([vix_closes, iwm_closes])
    return pd.DataFrame(data, index=idx, columns=cols)


class TestFetchRegimeInputs:
    def test_computes_vix_and_iwm_returns(self):
        # 25 IWM closes rising 1%/bar from 100; VIX flat at 18.
        iwm = [100.0 * (1.01 ** i) for i in range(25)]
        vix = [18.0] * 25
        yf = MagicMock()
        yf.get_daily_prices.return_value = _multiindex_frame(vix, iwm)

        ri = fetch_regime_inputs(yf)
        assert ri is not None
        assert ri.vix == pytest.approx(18.0)
        # 10d return = close[-1]/close[-11] - 1 = 1.01**10 - 1
        assert ri.iwm_10d_return == pytest.approx(1.01 ** 10 - 1, rel=1e-6)
        assert ri.iwm_20d_return == pytest.approx(1.01 ** 20 - 1, rel=1e-6)

    def test_none_on_failed_fetch(self):
        yf = MagicMock()
        yf.get_daily_prices.return_value = None
        assert fetch_regime_inputs(yf) is None

    def test_none_on_short_history(self):
        # Only 10 IWM closes — can't compute a 20-day return.
        yf = MagicMock()
        yf.get_daily_prices.return_value = _multiindex_frame([18.0] * 10, [100.0] * 10)
        assert fetch_regime_inputs(yf) is None

    def test_close_series_handles_single_level_frame(self):
        idx = pd.date_range("2026-01-01", periods=3, freq="B")
        df = pd.DataFrame({"Close": [1.0, 2.0, 3.0]}, index=idx)
        s = _close_series(df, "ANYTHING")
        assert list(s) == [1.0, 2.0, 3.0]

    def test_crash_in_downturn_inputs(self):
        # IWM falling 1%/bar → negative 10d/20d returns (would flag elevated/crisis).
        iwm = [100.0 * (0.99 ** i) for i in range(25)]
        vix = [40.0] * 25
        yf = MagicMock()
        yf.get_daily_prices.return_value = _multiindex_frame(vix, iwm)

        ri = fetch_regime_inputs(yf)
        assert ri.vix == pytest.approx(40.0)
        assert ri.iwm_20d_return < 0
