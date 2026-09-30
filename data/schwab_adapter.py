"""
Schwab API adapter wrapping schwab-py.

Handles authentication, rate limiting (≤120 req/min), retries, and error
logging. All methods return typed dataclasses or None on failure —
never raise to callers. Errors are logged internally.

Phase 0/1 methods (fully implemented):
  get_quotes_batch, get_price_history, get_option_chain, get_account_info

Phase 2+ stubs (log warning if called):
  place_order, cancel_order, get_order_status

Authentication:
  Use SchwabAdapter.from_token_file() for production.
  Pass a mock client to the constructor for testing.

Whitepaper reference: Section 5 (Execution Architecture)
Supplements reference: Doc 1, Section 1.1
"""

import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd

from data.utils import RateLimiter

logger = logging.getLogger(__name__)

# Max symbols per Schwab batch quotes call
_QUOTE_BATCH_SIZE = 100
# Max retries on 5xx errors
_MAX_RETRIES = 3
_RETRY_BACKOFF_S = 10.0
# Only aggregate near-term expirations (Doc 1 §1.1: "next 2 months"). Far-dated
# LEAPS carry large open interest that inflates totals past the universe OI
# floors and is unrelated to the near-term flow the options signal targets (F-36).
_MAX_OPTION_DTE_DAYS = 60


def _notify(message: str) -> None:
    """Send an operator Telegram alert if credentials are configured. Never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat_id):
        return
    try:
        payload = json.dumps({"chat_id": chat_id, "text": message}).encode()
        url = "https://api.telegram.org/bot" + token + "/sendMessage"
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        logger.warning("schwab_adapter: Telegram notification failed", exc_info=True)


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------

@dataclass
class QuoteData:
    """Normalised price/volume quote for a single symbol."""

    ticker: str
    last_price: float
    bid_price: float
    ask_price: float
    bid_ask_spread: float         # ask - bid
    bid_ask_spread_bps: float     # spread as fraction of mid, in basis points
    total_volume: int
    high_price: float
    low_price: float
    close_price: float            # prior close
    week_52_high: float
    week_52_low: float


@dataclass
class OptionChainSummary:
    """
    Aggregated options flow data for a single symbol.

    Extracted from the chain to support the options composite signal. Per Doc 1
    Section 1.1, aggregates only near-term expirations (≤ _MAX_OPTION_DTE_DAYS,
    ~2 months); far-dated LEAPS are excluded (F-36).

    IV units: all IV fields are in PERCENT (Schwab convention, e.g. 45.2 = 45.2%),
    so iv_range is in percentage points. compute_options_signal expects IV as a
    decimal — convert (÷100) when wiring IV rank.
    """

    ticker: str
    total_call_volume: int
    total_put_volume: int
    total_oi: int
    call_put_volume_ratio: float    # calls / (calls + puts); NaN if both zero
    strike_count_with_oi: int       # distinct strikes with OI > 0
    weighted_avg_iv: float          # volume-weighted IV across contracts (percent)
    iv_min: float                   # lowest IV across contracts (percent)
    iv_max: float                   # highest IV across contracts (percent)
    iv_range: float                 # iv_max - iv_min (percentage points)
    distinct_lot_sizes: int         # count of distinct daily volume buckets
    meets_volume_floor: bool        # total_call_volume + total_put_volume >= min
    meets_strike_floor: bool        # strike_count_with_oi >= min_strike_count


@dataclass
class AccountInfo:
    """Lightweight account summary from get_account_numbers()."""

    account_hash: str
    account_number: str


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class SchwabAdapter:
    """
    Adapter for the Schwab brokerage API via schwab-py.

    Args:
        client: A schwab-py Client instance. Use from_token_file() for
            production; pass a mock in tests.
        options_min_daily_volume: Minimum total option volume for a signal
            to be considered valid.
        options_min_strike_count: Minimum distinct strikes with OI > 0.
    """

    def __init__(
        self,
        client: Any,
        options_min_daily_volume: int = 200,
        options_min_strike_count: int = 4,
    ) -> None:
        self._client = client
        self._rate_limiter = RateLimiter(max_calls=110, period_seconds=60.0)  # 110/min buffer
        self._options_min_vol = options_min_daily_volume
        self._options_min_strikes = options_min_strike_count
        # Sticky flag: once a 401 proves the OAuth refresh token is dead, every
        # subsequent call short-circuits instead of retrying (F-13). Reset only
        # by reconstructing the adapter (i.e. after operator re-authentication).
        self._auth_failed = False
        logger.info("SchwabAdapter initialised")

    # ------------------------------------------------------------------
    # Auth-failure handling (F-13)
    # ------------------------------------------------------------------

    @property
    def auth_failed(self) -> bool:
        """True once a 401 has proven the session's Schwab token is invalid."""
        return self._auth_failed

    def _note_auth_failure(self, context: str) -> None:
        """
        Mark the Schwab token dead for the rest of this session and alert once.

        A 401 means the OAuth refresh token has expired (Schwab's lasts only
        7 days); no amount of retrying or backoff will fix it mid-session.
        Without this, a post-expiry scan retried each of ~40 chain/quote calls
        3× with 10 s sleeps — burning ~13+ minutes — and then "succeeded" with
        all-None Schwab data and no operator signal that reauth was needed.

        The first 401 flips a sticky flag (so the remaining calls in the scan
        return immediately) and fires a single CRITICAL Telegram alert.
        """
        if not self._auth_failed:
            self._auth_failed = True
            msg = (
                f"CRITICAL: Schwab token invalid (401) during {context} — "
                "re-authentication required. Schwab data (quotes / option "
                "chains / price history) is unavailable until the token is "
                "refreshed and the daemon restarted."
            )
            logger.critical(msg)
            _notify(msg)
        else:
            logger.debug("Schwab %s skipped — token already known-invalid", context)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_token_file(
        cls,
        api_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        token_path: Optional[str] = None,
        **kwargs: Any,
    ) -> "SchwabAdapter":
        """
        Create a SchwabAdapter from saved OAuth token file.

        Reads credentials from environment variables if not provided:
          SCHWAB_API_KEY, SCHWAB_APP_SECRET, SCHWAB_TOKEN_PATH

        Args:
            api_key: Schwab API key.
            app_secret: Schwab app secret.
            token_path: Path to token.json file.
            **kwargs: Forwarded to SchwabAdapter constructor.

        Returns:
            Configured SchwabAdapter.

        Raises:
            ImportError: If schwab package is not installed.
            RuntimeError: If token file does not exist.
        """
        try:
            import schwab
        except ImportError as exc:
            raise ImportError(
                "schwab-py is not installed. Run: pip install schwab-py"
            ) from exc

        key = api_key or os.environ.get("SCHWAB_API_KEY", "")
        secret = app_secret or os.environ.get("SCHWAB_APP_SECRET", "")
        token = token_path or os.environ.get("SCHWAB_TOKEN_PATH", "token.json")

        if not key or not secret:
            raise RuntimeError(
                "SCHWAB_API_KEY and SCHWAB_APP_SECRET must be set "
                "(as arguments or environment variables)."
            )

        client = schwab.auth.client_from_token_file(token, key, secret)
        return cls(client=client, **kwargs)

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

    def get_quotes_batch(self, symbols: list[str]) -> dict[str, Optional[QuoteData]]:
        """
        Return normalised quotes for a list of symbols.

        Batches requests into groups of _QUOTE_BATCH_SIZE (100). Symbols
        that fail or return no data are represented as None in the result.

        Args:
            symbols: List of ticker strings.

        Returns:
            Dict mapping ticker → QuoteData (or None if unavailable).
        """
        results: dict[str, Optional[QuoteData]] = {}
        for i in range(0, len(symbols), _QUOTE_BATCH_SIZE):
            batch = symbols[i : i + _QUOTE_BATCH_SIZE]
            batch_results = self._fetch_quotes(batch)
            results.update(batch_results)
        return results

    def _fetch_quotes(self, symbols: list[str]) -> dict[str, Optional[QuoteData]]:
        """Fetch quotes for a single batch of ≤100 symbols."""
        if self._auth_failed:
            return {s: None for s in symbols}
        self._rate_limiter.wait_if_needed()
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._client.get_quotes(symbols)
                if resp.status_code == 401:
                    self._note_auth_failure("get_quotes")
                    return {s: None for s in symbols}
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", 60))
                    logger.warning("Schwab rate limited — sleeping %.0fs", retry_after)
                    time.sleep(retry_after)
                    continue
                if resp.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_RETRY_BACKOFF_S)
                        continue
                    logger.error("Schwab quotes 5xx after %d retries", _MAX_RETRIES)
                    return {s: None for s in symbols}
                resp.raise_for_status()
                return self._parse_quotes(resp.json())
            except Exception as exc:
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF_S)
                    continue
                logger.error("Schwab get_quotes failed for batch: %s", exc)
                return {s: None for s in symbols}
        return {s: None for s in symbols}

    @staticmethod
    def _parse_quotes(data: dict) -> dict[str, Optional[QuoteData]]:
        """Parse Schwab get_quotes() response into QuoteData objects."""
        results: dict[str, Optional[QuoteData]] = {}
        for ticker, q in data.items():
            try:
                if isinstance(q, dict) and "quote" in q:
                    q = q["quote"]  # Some schwab-py versions nest under 'quote'
                bid = float(q.get("bidPrice", 0) or 0)
                ask = float(q.get("askPrice", 0) or 0)
                last = float(q.get("lastPrice", 0) or 0)
                mid = (bid + ask) / 2 if (bid + ask) > 0 else last
                spread = ask - bid
                spread_bps = (spread / mid * 10_000) if mid > 0 else 0.0
                # A zero last_price means the response had no useful price data
                if last == 0 and bid == 0 and ask == 0:
                    logger.debug("Quote for %s has no price data — skipping", ticker)
                    results[ticker] = None
                    continue
                results[ticker] = QuoteData(
                    ticker=ticker,
                    last_price=last,
                    bid_price=bid,
                    ask_price=ask,
                    bid_ask_spread=spread,
                    bid_ask_spread_bps=spread_bps,
                    total_volume=int(q.get("totalVolume", 0) or 0),
                    high_price=float(q.get("highPrice", 0) or 0),
                    low_price=float(q.get("lowPrice", 0) or 0),
                    close_price=float(q.get("closePrice", 0) or 0),
                    week_52_high=float(q.get("52WkHigh", 0) or 0),
                    week_52_low=float(q.get("52WkLow", 0) or 0),
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.debug("Quote parse error for %s: %s", ticker, exc)
                results[ticker] = None
        return results

    # ------------------------------------------------------------------
    # Price history
    # ------------------------------------------------------------------

    def get_price_history(
        self,
        symbol: str,
        period: str = "1y",
        frequency: str = "daily",
    ) -> Optional[pd.DataFrame]:
        """
        Return OHLCV price history as a DataFrame.

        Args:
            symbol: Ticker string.
            period: '1y', '2y', '6mo', '3mo', '1mo'.
            frequency: 'daily', '5min', '1min'.

        Returns:
            DataFrame with DatetimeIndex and [open, high, low, close, volume],
            or None on failure.
        """
        if self._auth_failed:
            return None
        try:
            import schwab
        except ImportError:
            logger.error("schwab-py not installed — cannot fetch price history")
            return None

        period_map = {
            "1y": (schwab.client.Client.PriceHistory.Period.ONE_YEAR,
                   schwab.client.Client.PriceHistory.PeriodType.YEAR),
            "2y": (schwab.client.Client.PriceHistory.Period.TWO_YEARS,
                   schwab.client.Client.PriceHistory.PeriodType.YEAR),
            "6mo": (schwab.client.Client.PriceHistory.Period.SIX_MONTHS,
                    schwab.client.Client.PriceHistory.PeriodType.MONTH),
            "3mo": (schwab.client.Client.PriceHistory.Period.THREE_MONTHS,
                    schwab.client.Client.PriceHistory.PeriodType.MONTH),
        }
        freq_map = {
            "daily": (schwab.client.Client.PriceHistory.Frequency.DAILY,
                      schwab.client.Client.PriceHistory.FrequencyType.DAILY),
            "5min":  (schwab.client.Client.PriceHistory.Frequency.EVERY_FIVE_MINUTES,
                      schwab.client.Client.PriceHistory.FrequencyType.MINUTE),
            "1min":  (schwab.client.Client.PriceHistory.Frequency.EVERY_MINUTE,
                      schwab.client.Client.PriceHistory.FrequencyType.MINUTE),
        }

        if period not in period_map:
            logger.warning("Unknown period %r for %s — defaulting to 1y", period, symbol)
            period = "1y"
        if frequency not in freq_map:
            logger.warning("Unknown frequency %r for %s — defaulting to daily", frequency, symbol)
            frequency = "daily"

        period_val, period_type = period_map[period]
        freq_val, freq_type = freq_map[frequency]

        self._rate_limiter.wait_if_needed()
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._client.get_price_history(
                    symbol,
                    period_type=period_type,
                    period=period_val,
                    frequency_type=freq_type,
                    frequency=freq_val,
                )
                if resp.status_code == 401:
                    self._note_auth_failure("get_price_history")
                    return None
                if resp.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_RETRY_BACKOFF_S)
                        continue
                    logger.error("Schwab price history 5xx for %s", symbol)
                    return None
                resp.raise_for_status()
                return self._parse_price_history(resp.json())
            except Exception as exc:
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF_S)
                    continue
                logger.error("Schwab price history failed for %s: %s", symbol, exc)
                return None
        return None

    @staticmethod
    def _parse_price_history(data: dict) -> Optional[pd.DataFrame]:
        """Parse Schwab price history response into a DataFrame."""
        candles = data.get("candles", [])
        if not candles:
            return None
        df = pd.DataFrame(candles)
        df["datetime"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
        df = df.set_index("datetime").rename(columns={
            "open": "open", "high": "high", "low": "low",
            "close": "close", "volume": "volume",
        })
        return df[["open", "high", "low", "close", "volume"]].sort_index()

    # ------------------------------------------------------------------
    # Options chain
    # ------------------------------------------------------------------

    def get_option_chain(self, symbol: str) -> Optional[OptionChainSummary]:
        """
        Return aggregated options flow summary for a symbol.

        Applies all safety checks:
          - Minimum daily volume floor (options_min_daily_volume)
          - Minimum distinct strikes with OI (options_min_strike_count)

        Args:
            symbol: Ticker string.

        Returns:
            OptionChainSummary dataclass, or None on API failure.
            If the chain exists but fails safety checks, returns the summary
            with meets_volume_floor=False / meets_strike_floor=False.
        """
        if self._auth_failed:
            return None
        self._rate_limiter.wait_if_needed()
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._client.get_option_chain(
                    symbol,
                    contract_type=self._client.__class__.Options.ContractType.ALL,
                )
                if resp.status_code == 401:
                    self._note_auth_failure("get_option_chain")
                    return None
                if resp.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_RETRY_BACKOFF_S)
                        continue
                    logger.error("Schwab options chain 5xx for %s", symbol)
                    return None
                resp.raise_for_status()
                return self._parse_option_chain(symbol, resp.json())
            except Exception as exc:
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF_S)
                    continue
                logger.error("Schwab options chain failed for %s: %s", symbol, exc)
                return None
        return None

    @staticmethod
    def _within_dte(exp_key: str, max_dte: int) -> bool:
        """
        Return True if an expiration key's days-to-expiry is within ``max_dte``.

        Schwab keys each expiration as ``"YYYY-MM-DD:DTE"`` (e.g.
        ``"2026-08-21:30"``). Keys that don't parse default to *included* so an
        unexpected format never silently drops the whole chain.
        """
        try:
            dte = int(exp_key.split(":")[1])
        except (IndexError, ValueError):
            return True
        return 0 <= dte <= max_dte

    def _parse_option_chain(self, symbol: str, data: dict) -> Optional[OptionChainSummary]:
        """Parse a Schwab options chain response into OptionChainSummary."""
        call_map: dict = data.get("callExpDateMap", {})
        put_map: dict = data.get("putExpDateMap", {})

        if not call_map and not put_map:
            logger.debug("Empty options chain for %s", symbol)
            return None

        total_call_vol = 0
        total_put_vol = 0
        total_oi = 0
        iv_weighted_sum = 0.0
        iv_weight_total = 0.0
        iv_values: list[float] = []
        strikes_with_oi: set[float] = set()
        call_vol_by_strike: dict[float, int] = {}
        put_vol_by_strike: dict[float, int] = {}

        for exp_map, is_call in [(call_map, True), (put_map, False)]:
            for _exp, strikes in exp_map.items():
                # Schwab keys each expiration as "YYYY-MM-DD:DTE". Skip far-dated
                # (LEAPS) expirations so aggregated OI/volume reflects near-term
                # flow only (F-36).
                if not self._within_dte(_exp, _MAX_OPTION_DTE_DAYS):
                    continue
                for strike_str, contracts in strikes.items():
                    for contract in contracts:
                        vol = int(contract.get("totalVolume", 0) or 0)
                        oi = int(contract.get("openInterest", 0) or 0)
                        iv = float(contract.get("volatility", 0) or 0)
                        strike = float(contract.get("strikePrice", 0) or 0)

                        if oi > 0:
                            strikes_with_oi.add(strike)
                        total_oi += oi

                        if is_call:
                            total_call_vol += vol
                            call_vol_by_strike[strike] = call_vol_by_strike.get(strike, 0) + vol
                        else:
                            total_put_vol += vol
                            put_vol_by_strike[strike] = put_vol_by_strike.get(strike, 0) + vol

                        # NOTE (F-36): Schwab reports `volatility` in PERCENT
                        # (e.g. 45.2 == 45.2%). weighted_avg_iv / iv_min / iv_max
                        # / iv_range below are therefore all in percentage points.
                        # compute_options_signal() documents current_iv "as
                        # decimal" — whoever wires IV rank MUST divide these by
                        # 100 first, or the iv_range floor check is 100× off.
                        if iv > 0 and vol > 0:
                            iv_weighted_sum += iv * vol
                            iv_weight_total += vol
                            iv_values.append(iv)

        weighted_iv = iv_weighted_sum / iv_weight_total if iv_weight_total > 0 else 0.0
        iv_min = min(iv_values) if iv_values else 0.0
        iv_max = max(iv_values) if iv_values else 0.0

        # Distinct lot sizes: count unique volume bucket sizes across strikes
        # (proxy for diversity of trade sources per spec)
        all_vols = list(call_vol_by_strike.values()) + list(put_vol_by_strike.values())
        vol_buckets = set(round(v, -1) for v in all_vols if v > 0)  # round to 10s
        distinct_lots = len(vol_buckets)

        total_vol = total_call_vol + total_put_vol
        return OptionChainSummary(
            ticker=symbol,
            total_call_volume=total_call_vol,
            total_put_volume=total_put_vol,
            total_oi=total_oi,
            call_put_volume_ratio=(
                total_call_vol / total_vol if total_vol > 0 else float("nan")
            ),
            strike_count_with_oi=len(strikes_with_oi),
            weighted_avg_iv=weighted_iv,
            iv_min=iv_min,
            iv_max=iv_max,
            iv_range=iv_max - iv_min,
            distinct_lot_sizes=distinct_lots,
            meets_volume_floor=total_vol >= self._options_min_vol,
            meets_strike_floor=len(strikes_with_oi) >= self._options_min_strikes,
        )

    # ------------------------------------------------------------------
    # Account (used as heartbeat / token validation)
    # ------------------------------------------------------------------

    def get_account_info(self) -> Optional[AccountInfo]:
        """
        Return basic account info. Used as a lightweight token validation heartbeat.

        Returns:
            AccountInfo for the first account, or None if auth fails.
        """
        if self._auth_failed:
            return None
        self._rate_limiter.wait_if_needed()
        try:
            resp = self._client.get_account_numbers()
            if resp.status_code == 401:
                self._note_auth_failure("get_account_info")
                return None
            resp.raise_for_status()
            accounts = resp.json()
            if not accounts:
                return None
            first = accounts[0]
            return AccountInfo(
                account_hash=first.get("hashValue", ""),
                account_number=first.get("accountNumber", ""),
            )
        except Exception as exc:
            logger.error("Schwab get_account_info failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Phase 2+ stubs (order management)
    # ------------------------------------------------------------------

    def place_order(self, account_hash: str, order_spec: Any) -> Optional[str]:
        """Phase 2+ — not yet implemented. Returns None."""
        logger.warning("place_order called but order execution is Phase 2+ only")
        return None

    def cancel_order(self, account_hash: str, order_id: str) -> bool:
        """Phase 2+ — not yet implemented. Returns False."""
        logger.warning("cancel_order called but order execution is Phase 2+ only")
        return False

    def get_order_status(self, account_hash: str, order_id: str) -> Optional[dict]:
        """Phase 2+ — not yet implemented. Returns None."""
        logger.warning("get_order_status called but order execution is Phase 2+ only")
        return None
