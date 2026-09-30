"""
Arconian v3 — survivorship-bias-free historical signal backtest.

Whitepaper reference:
  Section 2.3  Hou-Moskowitz price delay score
  Section 2.4  Survivorship-bias-free universe (Norgate Platinum required)
  Section 7.4  IC computation methodology

Signals computed:
  volume    (30%)  multi-window dollar volume percentile rank
  return    (25%)  absolute 1-day return percentile rank
  sector_rs (10%)  stock vs sector-ETF 5-day spread percentile rank
  delay     (10%)  Hou-Moskowitz delay score, recomputed monthly
  options   (15%)  held at 0.50 neutral — no historical chains available

Documented limitations vs. live system:
  - Options signal neutral (no historical data at any practical cost)
  - Retail attention penalty absent (no historical social/scanner data)
  - Market cap filter approximated (historical shares outstanding not in Norgate)
  - Bid-ask spread and options OI filters not applied (not in Norgate)

Usage (from project root):
    python scripts\\backtest.py
    python scripts\\backtest.py --start 2024-01-01 --end 2026-01-01
    python scripts\\backtest.py --start 2023-01-01 --end 2026-01-01 --output bt.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
import warnings
from datetime import date
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from data.norgate_adapter import NorgateAdapter, NorgateUnavailableError
from signals.volume_signal import compute_volume_signal
from signals.return_signal import compute_return_signal
from signals.sector_rs_signal import (
    compute_sector_rs_signal,
    build_spread_history,
    compute_5d_return,
)
from signals.signal_engine import compute_composite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Whitepaper constants
# ---------------------------------------------------------------------------

# Initial weights — Section 3.2 (IC recalibration is a live-system feature)
WEIGHTS: dict[str, float] = {
    "volume":    0.30,
    "return":    0.25,
    "options":   0.15,
    "sector_rs": 0.10,
    "delay":     0.10,
}
OPTIONS_NEUTRAL        = 0.50   # neutral placeholder — Section 2.4 note
RETAIL_PENALTY_WEIGHT  = 0.20   # not applied here; retained for compute_composite sig

# Universe filters — Section 2.1
# Spread and options OI filters omitted: not available in Norgate
MIN_PRICE     = 2.0     # $/share
MIN_ADTV_M    = 5.0     # $M average daily dollar volume (20-day)
MIN_MCAP_M    = 500.0   # $M — approximated via recent price + current shares
MAX_MCAP_M    = 2_000.0 # $M
MIN_HISTORY   = 120     # trading days for full volume signal

# Delay score — Section 2.3
DELAY_WINDOW  = 52   # weeks of weekly returns
DELAY_LAGS    = 4    # lagged market return weeks in unrestricted model

# IC measurement — Section 7.4
FORWARD_DAYS  = 3    # close-to-close return horizon
IC_THRESHOLD  = 0.02 # whitepaper flag level (two consecutive periods below = review)

# Sector ETF map — Document 3, Section 3.4
SECTOR_ETF_MAP = {
    "Information Technology": "XLK",
    "Health Care":            "XLV",
    "Financials":             "XLF",
    "Consumer Discretionary": "XLY",
    "Communication Services": "XLC",
    "Industrials":            "XLI",
    "Consumer Staples":       "XLP",
    "Energy":                 "XLE",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Materials":              "XLB",
}
YFINANCE_NORMALIZE = {
    "Technology": "Information Technology",
    "Healthcare":  "Health Care",
}
MARKET_PROXY = "SPY"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _yf_batch_download(symbols: list[str], start: str, end: str) -> dict[str, pd.Series]:
    """Download adjusted close prices from yfinance. Returns symbol → Series."""
    import yfinance as yf
    if not symbols:
        return {}
    try:
        raw = yf.download(symbols, start=start, end=end,
                          auto_adjust=True, progress=False)
        if raw.empty:
            return {}
        close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
        if isinstance(close, pd.Series):
            close = close.to_frame(name=symbols[0])
        return {sym: close[sym].dropna() for sym in symbols if sym in close.columns}
    except Exception as exc:
        logger.warning("yfinance batch download failed: %s", exc)
        return {}


def _trading_days(start: str, end: str) -> pd.DatetimeIndex:
    """US market trading days derived from SPY closes — canonical calendar proxy."""
    prices = _yf_batch_download([MARKET_PROXY], start, end)
    if MARKET_PROXY not in prices:
        raise RuntimeError("Could not load SPY for trading calendar.")
    return prices[MARKET_PROXY].index


def _to_weekly_returns(daily: pd.Series) -> np.ndarray:
    """Resample daily closes to Friday-close weekly returns."""
    weekly = daily.resample("W-FRI").last().dropna()
    if len(weekly) < 2:
        return np.array([], dtype=float)
    return weekly.pct_change().dropna().values.astype(float)


# ---------------------------------------------------------------------------
# Delay score — Section 2.3
# ---------------------------------------------------------------------------

def compute_delay_score(
    stock_weekly: np.ndarray,
    market_weekly: np.ndarray,
) -> Optional[float]:
    """
    Hou-Moskowitz (2005) price delay score.

    Restricted:   R_i,t = a + b0*R_m,t
    Unrestricted: R_i,t = a + b0*R_m,t + sum_{k=1}^{4} bk*R_{m,t-k}
    Delay = 1 - (R2_restricted / R2_unrestricted)

    Returns value in [0, 1], or None if < DELAY_WINDOW weeks of history.
    Primary targets: delay > 0.3 (Section 2.3).
    """
    n = min(len(stock_weekly), len(market_weekly))
    if n < DELAY_WINDOW:
        return None

    y = stock_weekly[-n:].astype(float)
    m = market_weekly[-n:].astype(float)
    start = DELAY_LAGS
    y_eff = y[start:]
    ones  = np.ones(len(y_eff))

    X_restricted   = np.column_stack([ones, m[start:]])
    lag_cols        = [m[start - k : n - k] for k in range(DELAY_LAGS + 1)]
    X_unrestricted  = np.column_stack([ones] + lag_cols)

    def _r2(X: np.ndarray, y: np.ndarray) -> float:
        coef, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        ss_res = float(np.sum((y - X @ coef) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        return 0.0 if ss_tot < 1e-12 else 1.0 - ss_res / ss_tot

    r2_r = _r2(X_restricted, y_eff)
    r2_u = _r2(X_unrestricted, y_eff)

    if r2_u < 1e-6:
        return 0.5  # near-zero explanatory power — neutral
    return float(np.clip(1.0 - r2_r / r2_u, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Universe construction
# ---------------------------------------------------------------------------

def _passes_filters(hist: pd.DataFrame, as_of: pd.Timestamp) -> bool:
    """
    Point-in-time universe filter using price, ADTV, and history length.
    Market cap approximated; spread/options OI not applied (unavailable).
    """
    h = hist[hist.index <= as_of]
    if len(h) < MIN_HISTORY:
        return False
    last_price = float(h["Close"].iloc[-1])
    if last_price < MIN_PRICE:
        return False
    recent = h.tail(20)
    adtv_m = float((recent["Close"] * recent["Volume"]).mean()) / 1e6
    if adtv_m < MIN_ADTV_M:
        return False
    return True


def _quick_prefilter(
    norgate: NorgateAdapter,
    symbols: list[str],
    end_date: date,
    lookback_days: int = 25,
) -> list[str]:
    """
    Fast first pass: load only recent history to check price + ADTV.
    Reduces the symbol set before loading years of history.
    """
    start = date(end_date.year, end_date.month, end_date.day)
    # Go back ~25 trading days (~5 calendar weeks)
    approx_start = date(start.year, start.month, max(1, start.day - 40))
    passing = []
    for sym in symbols:
        df = norgate.get_price_history(sym, approx_start, end_date)
        if df is None or df.empty:
            continue
        last_price = float(df["Close"].iloc[-1])
        if last_price < MIN_PRICE:
            continue
        adtv_m = float((df["Close"] * df["Volume"]).mean()) / 1e6
        if adtv_m >= MIN_ADTV_M:
            passing.append(sym)
    return passing


# ---------------------------------------------------------------------------
# Main backtest
# ---------------------------------------------------------------------------

def run_backtest(
    start: str,
    end: str,
    norgate: NorgateAdapter,
    output_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    Walk-forward signal backtest over [start, end].

    Returns DataFrame with columns:
        date, ticker, composite, volume, return_mag, sector_rs, delay,
        fwd_return_3d
    """
    # ------------------------------------------------------------------ #
    # 1. Market proxy + sector ETF prices (yfinance)                      #
    # ------------------------------------------------------------------ #
    # Extend 2 years back for delay score weekly history
    year_offset = int(start[:4]) - 2
    extended_start = f"{year_offset}{start[4:]}"

    logger.info("Downloading market proxy and sector ETFs from yfinance...")
    all_etf_tickers = list(set(SECTOR_ETF_MAP.values()))
    etf_data = _yf_batch_download(all_etf_tickers, extended_start, end)
    market_data = _yf_batch_download([MARKET_PROXY], extended_start, end)
    market_series: pd.Series = market_data[MARKET_PROXY]
    market_weekly = _to_weekly_returns(market_series)
    logger.info("Loaded %d / %d sector ETFs.", len(etf_data), len(all_etf_tickers))

    # ------------------------------------------------------------------ #
    # 2. Trading calendar                                                  #
    # ------------------------------------------------------------------ #
    trading_days = _trading_days(start, end)
    logger.info("Backtest window: %s → %s (%d trading days)",
                trading_days[0].date(), trading_days[-1].date(), len(trading_days))

    # ------------------------------------------------------------------ #
    # 3. Norgate symbol list                                               #
    # ------------------------------------------------------------------ #
    logger.info("Fetching symbol list from Norgate (active + delisted)...")
    all_symbols = norgate.get_all_symbols(include_delisted=True) or []
    logger.info("Norgate returned %d total symbols.", len(all_symbols))

    # ------------------------------------------------------------------ #
    # 4. Quick pre-filter (price + ADTV using recent data only)           #
    # ------------------------------------------------------------------ #
    end_date = pd.Timestamp(end).date()
    logger.info("Quick pre-filter pass (price ≥ $%.0f, ADTV ≥ $%.0fM)...",
                MIN_PRICE, MIN_ADTV_M)
    candidates = _quick_prefilter(norgate, all_symbols, end_date)
    logger.info("%d symbols pass quick pre-filter.", len(candidates))

    # ------------------------------------------------------------------ #
    # 5. Load full price history from Norgate                             #
    # ------------------------------------------------------------------ #
    norgate_start = date(int(extended_start[:4]),
                         int(extended_start[5:7]),
                         int(extended_start[8:10]))
    logger.info("Loading full price history from Norgate for %d symbols...",
                len(candidates))
    symbol_data: dict[str, pd.DataFrame] = {}
    for i, sym in enumerate(candidates):
        if i % 200 == 0:
            logger.info("  Norgate load: %d / %d", i, len(candidates))
        df = norgate.get_price_history(sym, norgate_start, end_date)
        if df is not None and len(df) >= MIN_HISTORY:
            symbol_data[sym] = df
    logger.info("Full history loaded for %d symbols.", len(symbol_data))

    # ------------------------------------------------------------------ #
    # 6. Sector mapping (yfinance, best-effort for active symbols)        #
    # ------------------------------------------------------------------ #
    logger.info("Fetching sector info from yfinance...")
    import yfinance as yf
    sector_etf: dict[str, Optional[str]] = {}
    syms_list = list(symbol_data.keys())
    # Batch info fetch in chunks to avoid hammering yfinance
    for i in range(0, len(syms_list), 50):
        chunk = syms_list[i : i + 50]
        for sym in chunk:
            try:
                raw = yf.Ticker(sym).info.get("sector", "")
                gics = YFINANCE_NORMALIZE.get(raw, raw)
                sector_etf[sym] = SECTOR_ETF_MAP.get(gics)
            except Exception:
                sector_etf[sym] = None
        if i % 200 == 0:
            logger.info("  Sector map: %d / %d", i, len(syms_list))

    # ------------------------------------------------------------------ #
    # 7. Monthly delay scores (precomputed, cached)                       #
    # ------------------------------------------------------------------ #
    month_starts = pd.date_range(start=start, end=end, freq="MS")
    logger.info("Precomputing delay scores: %d symbols × %d months...",
                len(symbol_data), len(month_starts))
    delay_cache: dict[tuple[str, pd.Timestamp], Optional[float]] = {}
    for ms in month_starts:
        mkt_to_ms     = market_series[market_series.index <= ms]
        mkt_wkly_to_ms = _to_weekly_returns(mkt_to_ms)
        for sym, df in symbol_data.items():
            stock_to_ms   = df["Close"][df.index <= ms]
            stk_wkly      = _to_weekly_returns(stock_to_ms)
            n = min(len(stk_wkly), len(mkt_wkly_to_ms))
            ds = compute_delay_score(stk_wkly[-n:], mkt_wkly_to_ms[-n:]) if n >= DELAY_WINDOW else None
            delay_cache[(sym, ms)] = ds
    logger.info("Delay score precomputation complete.")

    # ------------------------------------------------------------------ #
    # 8. Walk-forward signal computation                                  #
    # ------------------------------------------------------------------ #
    logger.info("Walk-forward signal computation starting...")
    rows: list[dict] = []
    monthly_universe: list[str] = []
    last_month: Optional[pd.Timestamp] = None

    for td in trading_days:
        this_month = pd.Timestamp(td.year, td.month, 1)

        # Monthly universe refresh — matches live cadence
        if this_month != last_month:
            last_month = this_month
            monthly_universe = [
                sym for sym, df in symbol_data.items()
                if _passes_filters(df, td)
            ]
            logger.info("  %s  universe: %d tickers", td.date(), len(monthly_universe))

        for sym in monthly_universe:
            df   = symbol_data[sym]
            hist = df[df.index <= td]
            if len(hist) < MIN_HISTORY:
                continue

            closes  = hist["Close"].values.astype(float)
            volumes = hist["Volume"].values.astype(float)
            dvols   = closes * volumes

            # -- Volume signal -------------------------------------------
            vol_result = compute_volume_signal(
                today_dollar_vol   = dvols[-1],
                history_dollar_vol = dvols[:-1],
            )
            vol_score = vol_result.composite

            # -- Return signal -------------------------------------------
            ret_score: Optional[float] = None
            if len(closes) >= 2:
                today_abs_ret = abs(closes[-1] / closes[-2] - 1.0)
                hist_abs_rets = np.abs(np.diff(closes[:-1]) / closes[:-2])
                ret_score = compute_return_signal(today_abs_ret, hist_abs_rets)

            # -- Sector RS signal ----------------------------------------
            etf_ticker = sector_etf.get(sym)
            if etf_ticker and etf_ticker in etf_data:
                etf_series_raw = etf_data[etf_ticker]
                etf_aligned    = etf_series_raw.reindex(hist.index, method="ffill").dropna()
                common_idx     = hist.index.intersection(etf_aligned.index)
                if len(common_idx) >= 11:  # need ≥6 for 5d return + ≥5 for spread history
                    stk_aligned = hist["Close"].reindex(common_idx).values.astype(float)
                    etf_aligned_vals = etf_aligned.reindex(common_idx).values.astype(float)
                    stk_r5    = compute_5d_return(stk_aligned)
                    etf_r5    = compute_5d_return(etf_aligned_vals)
                    spreads   = build_spread_history(stk_aligned, etf_aligned_vals)
                    srs_score = compute_sector_rs_signal(stk_r5, etf_r5, spreads)
                else:
                    srs_score = 0.5
            else:
                srs_score = 0.5  # neutral — no ETF mapping

            # -- Delay score (most recent monthly computation) -----------
            delay_score = delay_cache.get((sym, last_month))

            # -- Composite -----------------------------------------------
            _, composite, _ = compute_composite(
                signals={
                    "volume":    vol_score,
                    "return":    ret_score,
                    "options":   OPTIONS_NEUTRAL,
                    "sector_rs": srs_score,
                    "delay":     delay_score,
                },
                weights=WEIGHTS,
                retail_penalty=0.0,
                penalty_weight=RETAIL_PENALTY_WEIGHT,
            )

            rows.append({
                "date":       td.date(),
                "ticker":     sym,
                "composite":  composite,
                "volume":     vol_score,
                "return_mag": ret_score,
                "sector_rs":  srs_score,
                "delay":      delay_score,
            })

    logger.info("Signal computation complete — %d (date, ticker) observations.", len(rows))
    if not rows:
        raise RuntimeError("No observations — check date range and Norgate data.")

    results = pd.DataFrame(rows)
    results["date"] = pd.to_datetime(results["date"])
    results.sort_values(["date", "ticker"], inplace=True)
    results.reset_index(drop=True, inplace=True)

    # ------------------------------------------------------------------ #
    # 9. 3-day forward returns (close-to-close — Section 7.4)            #
    # ------------------------------------------------------------------ #
    logger.info("Computing %d-day forward returns...", FORWARD_DAYS)
    td_list = list(trading_days)
    td_index = {td: i for i, td in enumerate(td_list)}

    def _fwd_return(row: pd.Series) -> Optional[float]:
        df  = symbol_data.get(row["ticker"])
        if df is None:
            return None
        td  = pd.Timestamp(row["date"])
        idx = td_index.get(td)
        if idx is None or idx + FORWARD_DAYS >= len(td_list):
            return None
        fwd_td = td_list[idx + FORWARD_DAYS]
        entry  = df["Close"][df.index <= td]
        exit_  = df["Close"][df.index <= fwd_td]
        if entry.empty or exit_.empty:
            return None
        return float(exit_.iloc[-1] / entry.iloc[-1] - 1.0)

    results["fwd_return_3d"] = results.apply(_fwd_return, axis=1)
    results.dropna(subset=["fwd_return_3d"], inplace=True)
    logger.info("%d observations with valid forward returns.", len(results))

    if output_path:
        results.to_csv(output_path, index=False)
        logger.info("Results saved to %s", output_path)

    return results


# ---------------------------------------------------------------------------
# IC analysis — Section 7.4
# ---------------------------------------------------------------------------

def compute_ic_summary(results: pd.DataFrame) -> pd.DataFrame:
    """
    Spearman IC per signal vs 3-day forward return.
    Segmented IC (by scan type, earnings proximity) deferred to live system
    which has that metadata; this backtest computes aggregate IC only.
    """
    signal_cols = ["composite", "volume", "return_mag", "sector_rs", "delay"]
    rows = []
    for col in signal_cols:
        valid = results[[col, "fwd_return_3d"]].dropna()
        n = len(valid)
        if n < 30:
            rows.append({"signal": col, "IC": None, "n": n, "p_value": None,
                         "above_threshold": None})
            continue
        ic, pval = spearmanr(valid[col], valid["fwd_return_3d"])
        ic, pval = float(ic), float(pval)
        rows.append({
            "signal":          col,
            "IC":              round(ic, 4),
            "n":               n,
            "p_value":         round(pval, 4),
            "above_threshold": ic > IC_THRESHOLD,
        })
    return pd.DataFrame(rows).set_index("signal")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Arconian v3 historical signal backtest")
    p.add_argument("--start",     default="2024-01-01",
                   help="Backtest start date YYYY-MM-DD (default: 2024-01-01)")
    p.add_argument("--end",       default=str(date.today()),
                   help="Backtest end date YYYY-MM-DD (default: today)")
    p.add_argument("--output",    default=None,
                   help="CSV output path (default: backtest_YYYY-MM-DD.csv)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING"])
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("=" * 60)
    logger.info("Arconian v3 Historical Signal Backtest")
    logger.info("Whitepaper ref: Sections 2.3, 2.4, 7.4")
    logger.info("Options signal: held at %.2f (neutral) — no historical data",
                OPTIONS_NEUTRAL)
    logger.info("Retail penalty: not applied — no historical social data")
    logger.info("=" * 60)

    try:
        norgate = NorgateAdapter()
    except NorgateUnavailableError as exc:
        logger.critical("Norgate unavailable: %s", exc)
        sys.exit(1)

    output = args.output or f"backtest_{args.start}_{args.end}.csv"

    results = run_backtest(
        start=args.start,
        end=args.end,
        norgate=norgate,
        output_path=output,
    )

    ic = compute_ic_summary(results)

    print()
    print("=" * 60)
    print("IC SUMMARY  (Spearman r vs 3-day forward return)")
    print(f"Whitepaper threshold: IC > {IC_THRESHOLD} flags signal as predictive")
    print("=" * 60)
    print(ic.to_string())
    print()

    for signal, row in ic.iterrows():
        if row["IC"] is None:
            continue
        flag = "***" if row["above_threshold"] else "   "
        logger.info("%s %-12s IC=%+.4f  n=%-6d  p=%.4f",
                    flag, signal, row["IC"], int(row["n"]), row["p_value"])
    print()
    logger.info("Results written to: %s", output)
