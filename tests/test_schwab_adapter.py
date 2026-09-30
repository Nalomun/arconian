"""Tests for data/schwab_adapter.py."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.schwab_adapter import (
    SchwabAdapter,
    QuoteData,
    OptionChainSummary,
    _MAX_RETRIES,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _mock_response(data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


def make_adapter(**kwargs) -> SchwabAdapter:
    return SchwabAdapter(client=MagicMock(), **kwargs)


# ---------------------------------------------------------------------------
# Quote parsing
# ---------------------------------------------------------------------------

class TestParseQuotes:
    def test_parses_valid_quotes(self):
        raw = json.loads((FIXTURES / "schwab_quotes.json").read_text())
        result = SchwabAdapter._parse_quotes(raw)
        assert "MSTR" in result
        q = result["MSTR"]
        assert isinstance(q, QuoteData)
        assert q.last_price == pytest.approx(172.45)
        assert q.total_volume == 8342100

    def test_computes_spread_bps(self):
        raw = {"SOFI": {
            "lastPrice": 12.87, "bidPrice": 12.86, "askPrice": 12.88,
            "totalVolume": 22456780, "highPrice": 13.15, "lowPrice": 12.62,
            "closePrice": 12.95, "52WkHigh": 17.40, "52WkLow": 6.10
        }}
        result = SchwabAdapter._parse_quotes(raw)
        q = result["SOFI"]
        # Spread = 0.02, mid = 12.87, bps = 0.02/12.87 * 10000 ≈ 15.5
        assert q.bid_ask_spread == pytest.approx(0.02, abs=0.001)
        assert 14 < q.bid_ask_spread_bps < 17

    def test_returns_none_for_malformed_entry(self):
        raw = {"BAD": {"not_a_price": "garbage"}}
        result = SchwabAdapter._parse_quotes(raw)
        assert result["BAD"] is None

    def test_all_fixture_symbols_parsed(self):
        raw = json.loads((FIXTURES / "schwab_quotes.json").read_text())
        result = SchwabAdapter._parse_quotes(raw)
        for sym in ["MSTR", "SOFI", "IONQ", "HOOD"]:
            assert result[sym] is not None


# ---------------------------------------------------------------------------
# get_quotes_batch
# ---------------------------------------------------------------------------

class TestGetQuotesBatch:
    def test_batches_large_symbol_list(self):
        adapter = make_adapter()
        symbols = [f"SYM{i:03d}" for i in range(250)]
        call_count = 0

        def fake_get_quotes(batch):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {s: {
                "lastPrice": 10.0, "bidPrice": 9.99, "askPrice": 10.01,
                "totalVolume": 1000000, "highPrice": 10.5, "lowPrice": 9.5,
                "closePrice": 9.8, "52WkHigh": 15.0, "52WkLow": 5.0
            } for s in batch}
            resp.raise_for_status = MagicMock()
            return resp

        adapter._client.get_quotes.side_effect = fake_get_quotes
        result = adapter.get_quotes_batch(symbols)

        # 250 symbols / 100 per batch = 3 calls
        assert call_count == 3
        assert len(result) == 250

    def test_returns_none_on_5xx_after_retries(self):
        adapter = make_adapter()
        adapter._client.get_quotes.return_value = _mock_response({}, status_code=503)

        with patch("time.sleep"):  # Don't actually sleep in tests
            result = adapter.get_quotes_batch(["AAPL"])
        assert result["AAPL"] is None


# ---------------------------------------------------------------------------
# Option chain parsing
# ---------------------------------------------------------------------------

class TestParseOptionChain:
    def test_parses_fixture_chain(self):
        adapter = make_adapter(options_min_daily_volume=200, options_min_strike_count=4)
        raw = json.loads((FIXTURES / "schwab_options_chain.json").read_text())
        result = adapter._parse_option_chain("SOFI", raw)

        assert result is not None
        assert isinstance(result, OptionChainSummary)
        assert result.ticker == "SOFI"
        # 4 call strikes + 3 put strikes = 7 distinct strikes with OI
        assert result.strike_count_with_oi >= 4
        assert result.total_call_volume > 0
        assert result.total_put_volume > 0
        assert result.total_oi > 0
        assert 0 < result.weighted_avg_iv < 200  # IV in percentage terms

    def test_volume_floor_check(self):
        adapter = make_adapter(options_min_daily_volume=50000)  # Very high threshold
        raw = json.loads((FIXTURES / "schwab_options_chain.json").read_text())
        result = adapter._parse_option_chain("SOFI", raw)
        # Fixture has ~11,900 total volume — below 50,000
        assert result.meets_volume_floor is False

    def test_returns_none_on_empty_chain(self):
        adapter = make_adapter()
        result = adapter._parse_option_chain("EMPTY", {"callExpDateMap": {}, "putExpDateMap": {}})
        assert result is None

    def test_excludes_leaps_beyond_dte_bound(self):
        """F-36: far-dated (LEAPS) expirations must not inflate aggregated OI/vol."""
        def _contract(strike, vol, oi):
            return {"totalVolume": vol, "openInterest": oi,
                    "volatility": 40.0, "strikePrice": strike}

        chain = {
            "callExpDateMap": {
                "2026-08-21:30": {"50.0": [_contract(50.0, 100, 500)]},   # near-term
                "2028-01-21:600": {"50.0": [_contract(50.0, 999, 99999)]},  # LEAPS
            },
            "putExpDateMap": {},
        }
        adapter = make_adapter()
        result = adapter._parse_option_chain("X", chain)
        assert result is not None
        # Only the near-term contract is counted.
        assert result.total_call_volume == 100
        assert result.total_oi == 500

    def test_within_dte_parses_and_defaults(self):
        from data.schwab_adapter import _MAX_OPTION_DTE_DAYS
        assert SchwabAdapter._within_dte("2026-08-21:30", _MAX_OPTION_DTE_DAYS) is True
        assert SchwabAdapter._within_dte("2028-01-21:600", _MAX_OPTION_DTE_DAYS) is False
        # Unparseable key defaults to included (never silently drop a chain).
        assert SchwabAdapter._within_dte("weird-key", _MAX_OPTION_DTE_DAYS) is True


# ---------------------------------------------------------------------------
# Account info / heartbeat
# ---------------------------------------------------------------------------

class TestGetAccountInfo:
    def test_returns_account_info_on_success(self):
        adapter = make_adapter()
        adapter._client.get_account_numbers.return_value = _mock_response([
            {"accountNumber": "12345678", "hashValue": "abc123def456"}
        ])
        info = adapter.get_account_info()
        assert info is not None
        assert info.account_number == "12345678"
        assert info.account_hash == "abc123def456"

    def test_returns_none_on_401(self):
        adapter = make_adapter()
        resp = MagicMock()
        resp.status_code = 401
        resp.raise_for_status = MagicMock()
        adapter._client.get_account_numbers.return_value = resp
        result = adapter.get_account_info()
        assert result is None

    def test_returns_none_on_exception(self):
        adapter = make_adapter()
        adapter._client.get_account_numbers.side_effect = ConnectionError("network down")
        result = adapter.get_account_info()
        assert result is None


# ---------------------------------------------------------------------------
# F-13 — 401 auth-failure discrimination
# ---------------------------------------------------------------------------

class TestAuthFailureHandling:
    def test_quotes_401_does_not_retry_or_sleep(self):
        """A 401 is not a transient blip — no 3× retry, no backoff sleeps."""
        adapter = make_adapter()
        adapter._client.get_quotes.return_value = _mock_response({}, status_code=401)

        with patch("time.sleep") as sleep, patch("data.schwab_adapter._notify"):
            result = adapter.get_quotes_batch(["AAPL"])

        assert result["AAPL"] is None
        # Single attempt (5xx path would be 3); zero backoff sleeps.
        assert adapter._client.get_quotes.call_count == 1
        sleep.assert_not_called()
        assert adapter.auth_failed is True

    def test_401_short_circuits_subsequent_schwab_calls(self):
        """Once the token is known-dead, later calls return immediately."""
        adapter = make_adapter()
        adapter._client.get_quotes.return_value = _mock_response({}, status_code=401)

        with patch("data.schwab_adapter._notify"):
            adapter.get_quotes_batch(["AAPL"])
            # These must not hit the network at all.
            chain = adapter.get_option_chain("MSTR")
            hist = adapter.get_price_history("MSTR")
            quotes2 = adapter.get_quotes_batch(["NVDA"])

        assert chain is None
        assert hist is None
        assert quotes2["NVDA"] is None
        adapter._client.get_option_chain.assert_not_called()
        adapter._client.get_price_history.assert_not_called()
        # get_quotes only ever called for the first batch.
        assert adapter._client.get_quotes.call_count == 1

    def test_401_alerts_operator_exactly_once(self):
        """A dead token fires one CRITICAL alert, not one per call."""
        adapter = make_adapter()
        adapter._client.get_quotes.return_value = _mock_response({}, status_code=401)
        adapter._client.get_option_chain.return_value = _mock_response({}, status_code=401)

        with patch("data.schwab_adapter._notify") as notify:
            adapter.get_quotes_batch(["AAPL"])
            adapter.get_option_chain("MSTR")
            adapter.get_quotes_batch(["NVDA"])

        assert notify.call_count == 1

    def test_option_chain_401_sets_flag(self):
        # get_option_chain reads self._client.__class__.Options.ContractType.ALL,
        # so the client's *class* must expose that enum (a bare MagicMock class
        # does not). Use a small fake client whose class carries it.
        class _Options:
            class ContractType:
                ALL = "ALL"

        class _FakeClient:
            Options = _Options

            def __init__(self):
                self.get_option_chain = MagicMock(
                    return_value=_mock_response({}, status_code=401)
                )

        client = _FakeClient()
        adapter = SchwabAdapter(client=client)

        with patch("time.sleep") as sleep, patch("data.schwab_adapter._notify"):
            result = adapter.get_option_chain("SOFI")

        assert result is None
        assert adapter.auth_failed is True
        assert client.get_option_chain.call_count == 1
        sleep.assert_not_called()

    def test_5xx_still_retries_and_does_not_set_auth_failed(self):
        """Transient 5xx behaviour is unchanged — retries, no auth flag."""
        adapter = make_adapter()
        adapter._client.get_quotes.return_value = _mock_response({}, status_code=503)

        with patch("time.sleep"):
            result = adapter.get_quotes_batch(["AAPL"])

        assert result["AAPL"] is None
        assert adapter._client.get_quotes.call_count == _MAX_RETRIES
        assert adapter.auth_failed is False


# ---------------------------------------------------------------------------
# Phase 2+ stubs return None/False
# ---------------------------------------------------------------------------

def test_place_order_is_stub():
    adapter = make_adapter()
    assert adapter.place_order("hash", {}) is None

def test_cancel_order_is_stub():
    adapter = make_adapter()
    assert adapter.cancel_order("hash", "order123") is False

def test_get_order_status_is_stub():
    adapter = make_adapter()
    assert adapter.get_order_status("hash", "order123") is None
