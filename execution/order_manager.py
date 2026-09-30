"""
Order Manager — paper trade lifecycle for Phase 1.

Manages the full lifecycle of paper trades:
  open_paper_trade()         create a Trade row linked to a signal_log row
  check_open_trades()        walk EOD prices and close trades at barriers
  close_paper_trade_manual() operator-initiated close

Paper trades are the entire point of Phase 1. _assert_paper_trading_disabled()
at the bottom of this file is reserved for Phase 2+ live order submission.

Whitepaper reference: §4.2 (position sizing), §4.6 (exit rules), §11 (Phase 1)
"""

import json
import logging
import os
import urllib.request
from datetime import date, datetime, time as _time, timedelta
from math import floor
from typing import Callable, Optional

import pytz

logger = logging.getLogger(__name__)

_ET = pytz.timezone("US/Eastern")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _notify(message: str) -> None:
    """Send a Telegram message. Never raises."""
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
        logger.warning("order_manager: Telegram notification failed", exc_info=True)


def _extract_ohlc_open(hist) -> list[tuple]:
    """
    Convert a yfinance DataFrame to a sorted list of (date, open, high, low, close).
    Handles both MultiIndex (multi-ticker) and single-ticker DataFrames.
    If Open is absent or NaN, falls back to that day's Close.
    """
    import pandas as pd

    if isinstance(hist.columns, pd.MultiIndex):
        ticker_label = hist.columns.get_level_values(1)[0]
        df = hist.xs(ticker_label, axis=1, level=1)
    else:
        df = hist

    df = df.sort_index()
    result = []
    for idx, row in df.iterrows():
        d = idx.date() if hasattr(idx, "date") else idx
        h = float(row.get("High",  row.get("high",  float("nan"))))
        l = float(row.get("Low",   row.get("low",   float("nan"))))
        c = float(row.get("Close", row.get("close", float("nan"))))
        o_raw = row.get("Open", row.get("open", float("nan")))
        o = float(o_raw) if o_raw == o_raw else c  # NaN → use close as proxy
        if any(v != v for v in (h, l, c)):  # NaN check on required fields
            continue
        result.append((d, o, h, l, c))
    return result


def _compute_sizing(
    entry_price: float,
    direction: str,
    account_equity: float,
    atr_20: float,
    catalyst_flag: bool,
    config,
) -> tuple:
    """
    Return (stop_price, target_price, shares, stop_distance, position_capped).

    Implements whitepaper §4.2 sizing with Phase 1 risk budget.
    Raises ValueError if shares size to zero after sizing or capping.
    """
    risk_per_trade = account_equity * config.risk.phase1_risk_per_trade
    atr_multiplier = config.risk.atr_stop_multiplier * (
        config.risk.catalyst_atr_premium if catalyst_flag else 1.0
    )
    stop_distance = atr_multiplier * atr_20

    if direction == "long":
        stop_price = entry_price - stop_distance
        target_price = entry_price + config.execution.tp1_r_multiple * stop_distance
    else:
        stop_price = entry_price + stop_distance
        target_price = entry_price - config.execution.tp1_r_multiple * stop_distance

    shares = floor(risk_per_trade / stop_distance)
    if shares == 0:
        raise ValueError(
            f"Position sized to zero shares — risk/ATR too small "
            f"(risk_per_trade={risk_per_trade:.2f}, stop_distance={stop_distance:.4f})"
        )

    position_capped = False
    max_position_value = account_equity * config.risk.max_position_pct
    if shares * entry_price > max_position_value:
        shares = floor(max_position_value / entry_price)
        position_capped = True
        logger.info(
            "order_manager: position capped — shares reduced to %d "
            "(max_position_value=%.2f, entry=%.4f)",
            shares, max_position_value, entry_price,
        )
        if shares == 0:
            raise ValueError(
                f"Position capped to zero shares — entry price too high relative to "
                f"max position (entry={entry_price:.4f}, max_position_value={max_position_value:.2f})"
            )

    return stop_price, target_price, float(shares), stop_distance, position_capped


def _close_trade_in_session(
    session,
    trade,
    exit_price: float,
    exit_time: datetime,
    exit_reason: str,
) -> None:
    """
    Write exit fields on trade and propagate to linked signal_log rows.
    Handles both single-leg closes and the trailing leg of a TP1 trade.
    Caller is responsible for session commit.
    """
    from models import SignalLog

    stop_distance = abs((trade.entry_price or 0.0) - (trade.stop_price or 0.0))

    if trade.tp1_hit:
        # Trailing leg: P&L on remaining shares + add TP1 leg.
        remaining = trade.shares_remaining or 0.0
        tp1_pnl = trade.tp1_realized_pnl or 0.0
        if trade.direction == "long":
            trailing_pnl = (exit_price - (trade.entry_price or 0.0)) * remaining
        else:
            trailing_pnl = ((trade.entry_price or 0.0) - exit_price) * remaining
        realized_pnl = tp1_pnl + trailing_pnl
        realized_r = (
            realized_pnl / (stop_distance * (trade.shares or 1.0))
            if stop_distance else 0.0
        )
    else:
        if trade.direction == "long":
            realized_pnl = (exit_price - (trade.entry_price or 0.0)) * (trade.shares or 0.0)
            realized_r = (
                (exit_price - trade.entry_price) / stop_distance
                if stop_distance and trade.entry_price is not None
                else 0.0
            )
        else:
            realized_pnl = ((trade.entry_price or 0.0) - exit_price) * (trade.shares or 0.0)
            realized_r = (
                (trade.entry_price - exit_price) / stop_distance
                if stop_distance and trade.entry_price is not None
                else 0.0
            )

    trade.exit_price = exit_price
    trade.exit_time = exit_time
    trade.exit_reason = exit_reason
    trade.status = "closed"
    trade.realized_pnl = realized_pnl
    trade.realized_r_multiple = realized_r

    signal_rows = session.query(SignalLog).filter(SignalLog.trade_id == trade.id).all()
    for sig in signal_rows:
        sig.exit_price = exit_price
        sig.realized_pnl = realized_pnl
        sig.realized_r_multiple = realized_r


def _do_close(
    trade,
    session_scope: Callable,
    exit_price: float,
    exit_time: datetime,
    exit_reason: str,
) -> None:
    """Open a fresh session, re-fetch the trade, close it, and commit."""
    from models import Trade

    with session_scope() as session:
        t = session.get(Trade, trade.id)
        if t is None or t.status != "open":
            return
        _close_trade_in_session(session, t, exit_price, exit_time, exit_reason)

    logger.info(
        "order_manager: trade closed — id=%d %s %s @ %.4f reason=%s",
        trade.id, trade.direction, trade.ticker, exit_price, exit_reason,
    )


def _build_close_notification(trade, exit_price: float, exit_reason: str) -> str:
    """Build the Telegram close message string (uses detached trade attributes)."""
    stop_distance = abs((trade.entry_price or 0.0) - (trade.stop_price or 0.0))
    shares = trade.shares or 0.0

    if trade.tp1_hit:
        remaining = trade.shares_remaining or 0.0
        tp1_pnl = trade.tp1_realized_pnl or 0.0
        if trade.direction == "long":
            trailing_pnl = (exit_price - (trade.entry_price or 0.0)) * remaining
        else:
            trailing_pnl = ((trade.entry_price or 0.0) - exit_price) * remaining
        pnl = tp1_pnl + trailing_pnl
        r_multiple = pnl / (stop_distance * shares) if (stop_distance and shares) else 0.0
    elif trade.direction == "long":
        r_multiple = (exit_price - (trade.entry_price or 0.0)) / stop_distance if stop_distance else 0.0
        pnl = (exit_price - (trade.entry_price or 0.0)) * shares
    else:
        r_multiple = ((trade.entry_price or 0.0) - exit_price) / stop_distance if stop_distance else 0.0
        pnl = ((trade.entry_price or 0.0) - exit_price) * shares

    emoji = "✅" if pnl >= 0 else "❌"
    return (
        f"{emoji} Paper {trade.ticker} closed: {exit_reason} @ ${exit_price:.2f} | "
        f"R={r_multiple:.2f} | P&L ${pnl:.2f}"
    )


def _record_tp1_in_session(
    session,
    trade_id: int,
    shares_remaining: float,
    trailing_stop_price: float,
    tp1_pnl: float,
) -> None:
    """Commit TP1 partial exit state to the trades row. Caller owns the session."""
    from models import Trade
    t = session.get(Trade, trade_id)
    if t:
        t.tp1_hit = True
        t.shares_remaining = shares_remaining
        t.trailing_stop_price = trailing_stop_price
        t.tp1_realized_pnl = tp1_pnl


def _update_trailing_stop_in_session(session, trade_id: int, new_price: float) -> None:
    """Ratchet the trailing stop price up (long) or down (short). Caller owns the session."""
    from models import Trade
    t = session.get(Trade, trade_id)
    if t:
        t.trailing_stop_price = new_price


def _check_one_trade(
    trade,
    session_scope: Callable,
    yf_adapter,
    as_of_date: date,
    config,
) -> Optional[str]:
    """
    Check a single open trade against EOD price data.

    Implements whitepaper §4.6 two-stage exit:
      - Before TP1: check stop → target (→ TP1 partial) → time stop.
      - After TP1: check trailing stop → time stop; ratchet trailing stop if price improves.

    Returns:
        'stop'          — full close at stop loss
        'trailing_stop' — full close at trailing stop (after TP1)
        'time_stop'     — full close at time stop
        'tp1'           — TP1 partial exit recorded this run, trade still open
        None            — no change
    """
    tp1_exit_pct = config.execution.tp1_exit_pct
    trailing_stop_atr_mult = config.execution.trailing_stop_atr_multiple
    time_stop_days = config.execution.time_stop_days

    entry_date = trade.entry_time.date() if trade.entry_time else as_of_date

    fetch_start = entry_date - timedelta(days=1)
    fetch_end = as_of_date + timedelta(days=7)

    try:
        hist = yf_adapter.get_price_history(
            ticker=trade.ticker,
            start=fetch_start.strftime("%Y-%m-%d"),
            end=fetch_end.strftime("%Y-%m-%d"),
            interval="1d",
        )
    except Exception:
        logger.warning(
            "order_manager: price fetch failed for %s trade_id=%d",
            trade.ticker, trade.id, exc_info=True,
        )
        return None

    if hist is None or hist.empty:
        logger.debug(
            "order_manager: no price data for %s trade_id=%d",
            trade.ticker, trade.id,
        )
        return None

    all_days = _extract_ohlc_open(hist)
    # Include the entry day (F-19): excluding it let an entry-day stop blow-through
    # go unseen until the next bar — an optimistic bias that inflated expectancy.
    # The entry bar is evaluated pessimistically below (adverse breach only).
    check_days = [
        (d, o, h, l, c)
        for (d, o, h, l, c) in all_days
        if entry_date <= d <= as_of_date
    ]
    if not check_days:
        return None

    stop_price = trade.stop_price
    target_price = trade.target_price
    is_long = trade.direction == "long"
    tp1_happened_this_run = False

    for i, (d, o, h, l, c) in enumerate(check_days):
        # i == 0 is the entry day → 0 full days held; first full day is i == 1.
        days_held = i
        exit_dt = datetime.combine(d, datetime.min.time())

        if d == entry_date:
            # Pessimistic entry-day evaluation (F-19). We entered intraday at
            # entry_price and cannot tell from an EOD bar whether the day's high/
            # low happened before or after entry. Honor only an ADVERSE breach
            # (the stop) and ignore the favorable side (target/TP1) and the time
            # stop on the entry bar — matching the "pessimistic on ambiguous
            # bars" convention used elsewhere. TP1 cannot have happened yet, so
            # only the pre-TP1 stop applies.
            entry_stop_hit = (l <= stop_price) if is_long else (h >= stop_price)
            if entry_stop_hit:
                _do_close(trade, session_scope, stop_price, exit_dt, "stop")
                _notify(_build_close_notification(trade, stop_price, "stop"))
                return "stop"
            continue

        if not trade.tp1_hit:
            stop_hit = (l <= stop_price) if is_long else (h >= stop_price)
            target_hit = (h >= target_price) if is_long else (l <= target_price)

            if stop_hit and target_hit:
                logger.warning(
                    "order_manager: ambiguous_exit on %s for trade_id=%d %s "
                    "(stop and target both breached); recording as stop (pessimistic)",
                    d, trade.id, trade.ticker,
                )
                _do_close(trade, session_scope, stop_price, exit_dt, "stop")
                _notify(_build_close_notification(trade, stop_price, "stop"))
                return "stop"

            if stop_hit:
                _do_close(trade, session_scope, stop_price, exit_dt, "stop")
                _notify(_build_close_notification(trade, stop_price, "stop"))
                return "stop"

            if target_hit:
                tp1_shares = floor((trade.shares or 0.0) * tp1_exit_pct)
                remaining_shares = (trade.shares or 0.0) - tp1_shares

                # Guard: if shares are too few to split, close full position.
                if tp1_shares == 0 or remaining_shares <= 0:
                    _do_close(trade, session_scope, target_price, exit_dt, "target")
                    _notify(_build_close_notification(trade, target_price, "target"))
                    return "target"

                if is_long:
                    tp1_pnl = (target_price - (trade.entry_price or 0.0)) * tp1_shares
                    initial_trailing = target_price - (trade.atr_at_entry or 0.0) * trailing_stop_atr_mult
                else:
                    tp1_pnl = ((trade.entry_price or 0.0) - target_price) * tp1_shares
                    initial_trailing = target_price + (trade.atr_at_entry or 0.0) * trailing_stop_atr_mult

                with session_scope() as session:
                    _record_tp1_in_session(session, trade.id, remaining_shares, initial_trailing, tp1_pnl)

                # Update in-memory for remaining iterations in this call.
                trade.tp1_hit = True
                trade.shares_remaining = remaining_shares
                trade.trailing_stop_price = initial_trailing
                trade.tp1_realized_pnl = tp1_pnl
                tp1_happened_this_run = True

                logger.info(
                    "order_manager: TP1 partial exit — id=%d %s @ %.4f | "
                    "tp1_shares=%d remaining=%d trailing_stop=%.4f",
                    trade.id, trade.ticker, target_price,
                    int(tp1_shares), int(remaining_shares), initial_trailing,
                )
                _notify(
                    f"Paper {trade.ticker} TP1: {int(tp1_shares)} shares @ ${target_price:.2f} "
                    f"P&L ${tp1_pnl:.2f} | trailing stop ${initial_trailing:.2f}"
                )
                continue  # check trailing stop on subsequent days

            if days_held >= time_stop_days:
                if i + 1 < len(check_days):
                    next_d, next_o, _, _, _ = check_days[i + 1]
                    time_exit_price = next_o
                    time_exit_dt = datetime.combine(next_d, datetime.min.time())
                else:
                    time_exit_price = c
                    time_exit_dt = exit_dt
                _do_close(trade, session_scope, time_exit_price, time_exit_dt, "time_stop")
                _notify(_build_close_notification(trade, time_exit_price, "time_stop"))
                return "time_stop"

        else:  # TP1 already hit — manage trailing stop on remaining shares
            trailing_stop = trade.trailing_stop_price
            trailing_hit = (l <= trailing_stop) if is_long else (h >= trailing_stop)

            if trailing_hit:
                _do_close(trade, session_scope, trailing_stop, exit_dt, "trailing_stop")
                _notify(_build_close_notification(trade, trailing_stop, "trailing_stop"))
                return "trailing_stop"

            # Ratchet trailing stop if price moved favorably.
            if trade.atr_at_entry:
                if is_long:
                    new_trailing = c - trade.atr_at_entry * trailing_stop_atr_mult
                    if new_trailing > trailing_stop:
                        with session_scope() as session:
                            _update_trailing_stop_in_session(session, trade.id, new_trailing)
                        trade.trailing_stop_price = new_trailing
                else:
                    new_trailing = c + trade.atr_at_entry * trailing_stop_atr_mult
                    if new_trailing < trailing_stop:
                        with session_scope() as session:
                            _update_trailing_stop_in_session(session, trade.id, new_trailing)
                        trade.trailing_stop_price = new_trailing

            if days_held >= time_stop_days:
                if i + 1 < len(check_days):
                    next_d, next_o, _, _, _ = check_days[i + 1]
                    time_exit_price = next_o
                    time_exit_dt = datetime.combine(next_d, datetime.min.time())
                else:
                    time_exit_price = c
                    time_exit_dt = exit_dt
                _do_close(trade, session_scope, time_exit_price, time_exit_dt, "time_stop")
                _notify(_build_close_notification(trade, time_exit_price, "time_stop"))
                return "time_stop"

    return "tp1" if tp1_happened_this_run else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def open_paper_trade(
    session_scope: Callable,
    signal_id: int,
    entry_price: float,
    entry_time: datetime,
    direction: str,
    account_equity: float,
    atr_20: float,
    catalyst_flag: bool,
    config,
    risk_spec=None,
):
    """
    Create a Trade row linked to the signal_log row.

    Computes stop, target, and share count per whitepaper §4.2 and §4.6.
    Writes entry_price, stop_price, target_price, shares, atr_at_entry.
    Sets status='open'. Sets signal_log.was_traded=True and trade_id.

    Args:
        risk_spec: Optional RiskEngine PositionSpec. When provided (full-engine
            enforce mode, #18d), the trade opens with the engine's already-sized
            shares/stop/target — which include the regime and circuit-breaker
            risk multipliers — instead of re-running the local _compute_sizing.

    Raises:
        ValueError: direction not in {long, short}, atr_20 <= 0,
                    entry_price <= 0, account_equity <= 0,
                    position sizes to zero shares.
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
    if atr_20 <= 0:
        raise ValueError(f"atr_20 must be positive, got {atr_20}")
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive, got {entry_price}")
    if account_equity <= 0:
        raise ValueError(f"account_equity must be positive, got {account_equity}")

    if risk_spec is not None:
        # Enforce mode: trust the engine's regime/CB-adjusted sizing verbatim.
        shares = risk_spec.shares
        stop_price = risk_spec.stop_price
        target_price = risk_spec.target_price_1
        position_capped = False
    else:
        stop_price, target_price, shares, _stop_dist, position_capped = _compute_sizing(
            entry_price=entry_price,
            direction=direction,
            account_equity=account_equity,
            atr_20=atr_20,
            catalyst_flag=catalyst_flag,
            config=config,
        )

    from models import SignalLog, Trade

    trade_id: Optional[int] = None
    ticker: Optional[str] = None

    with session_scope() as session:
        signal = session.get(SignalLog, signal_id)
        if signal is None:
            raise ValueError(f"signal_id {signal_id} not found in signal_log")

        ticker = signal.ticker
        trade = Trade(
            ticker=ticker,
            direction=direction,
            status="open",
            entry_price=entry_price,
            entry_time=entry_time,
            shares=shares,
            stop_price=stop_price,
            target_price=target_price,
            atr_at_entry=atr_20,
        )
        session.add(trade)
        session.flush()
        trade_id = trade.id

        signal.was_traded = True
        signal.trade_id = trade_id
        signal.entry_price = entry_price

    logger.info(
        "order_manager: paper trade opened — id=%d %s %s @ %.4f | "
        "stop=%.4f | target=%.4f | shares=%d%s",
        trade_id, direction, ticker, entry_price,
        stop_price, target_price, int(shares),
        " [CAPPED]" if position_capped else "",
    )

    _notify(
        f"\U0001f4dd Paper {direction} {ticker} @ ${entry_price:.2f} | "
        f"stop ${stop_price:.2f} | target ${target_price:.2f} | {int(shares)} shares"
    )

    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        session.expunge(trade)
    return trade


def check_open_trades(
    session_scope: Callable,
    yf_adapter,
    as_of_date: date,
    config,
) -> dict:
    """
    For every Trade with status='open':
      1. Fetch EOD OHLC for every trading day since entry_time (via yf_adapter).
      2. Walk day by day checking in this order per whitepaper §4.6:
         a. Stop hit (low <= stop for long, high >= stop for short)
            → close at stop_price, exit_reason='stop'.
         b. Target hit (high >= target for long, low <= target for short)
            → close at target_price, exit_reason='target'.
         c. Time stop: days held >= config.execution.time_stop_days
            → close at next session's open (or current close if no next day),
            exit_reason='time_stop'.
      3. Close: set exit_price/exit_time/exit_reason/status='closed',
         compute realized_pnl and realized_r_multiple, propagate to signal_log.
      4. If both stop and target possible on same day, record as 'stop'
         (pessimistic — intraday order unknown from EOD data). Logs ambiguous_exit.

    Returns:
        {processed, closed_stop, closed_target, closed_time, errors}
    """
    from models import Trade

    with session_scope() as session:
        open_trades = session.query(Trade).filter(Trade.status == "open").all()
        for t in open_trades:
            session.expunge(t)

    processed = closed_stop = closed_trailing = closed_time = tp1_partial = errors = 0

    for trade in open_trades:
        processed += 1
        try:
            result = _check_one_trade(trade, session_scope, yf_adapter, as_of_date, config)
            if result == "stop":
                closed_stop += 1
            elif result == "trailing_stop":
                closed_trailing += 1
            elif result == "time_stop":
                closed_time += 1
            elif result == "tp1":
                tp1_partial += 1
        except Exception:
            logger.error(
                "order_manager: check_open_trades failed for trade_id=%d ticker=%s",
                trade.id, trade.ticker, exc_info=True,
            )
            errors += 1

    logger.info(
        "order_manager: check_open_trades — "
        "processed=%d stop=%d trailing=%d time=%d tp1=%d errors=%d",
        processed, closed_stop, closed_trailing, closed_time, tp1_partial, errors,
    )
    return {
        "processed": processed,
        "closed_stop": closed_stop,
        "closed_trailing": closed_trailing,
        "closed_time": closed_time,
        "tp1_partial": tp1_partial,
        "errors": errors,
    }


def close_paper_trade_manual(
    session_scope: Callable,
    trade_id: int,
    exit_price: float,
    exit_time: datetime,
    reason: str = "manual",
):
    """
    Operator-initiated close. Writes exit fields and status='closed'.

    Returns the closed Trade row (detached).
    Raises ValueError if trade not found or not open.
    """
    from models import Trade

    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        if trade is None:
            raise ValueError(f"trade_id {trade_id} not found")
        if trade.status != "open":
            raise ValueError(
                f"trade_id {trade_id} is not open (status={trade.status!r})"
            )
        _close_trade_in_session(session, trade, exit_price, exit_time, reason)

    logger.info(
        "order_manager: manual close — id=%d %s %s @ %.4f reason=%s",
        trade_id, trade.direction, trade.ticker, exit_price, reason,
    )

    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        session.expunge(trade)
    return trade


def _fetch_entry_price(yf_adapter, ticker: str) -> Optional[float]:
    """
    Return the entry price for a paper trade.

    Two-stage fallback because yfinance's fast_info["lastPrice"] silently
    returns None for thinly-traded small caps outside regular trading hours
    — which is most of our universe whenever a scan fires off-hours. Without
    a fallback, every off-hours scan dropped 100% of qualifying candidates
    (verified: 2026-04-22 22:38 — 50/50 skipped, no trades opened).

    1. Live fast_info price (best when market open)
    2. Most recent daily close from get_price_history (works any time)
    """
    try:
        price = yf_adapter.get_latest_price(ticker)
        if price is not None and price > 0:
            return float(price)
    except Exception:
        logger.debug(
            "auto_paper_trade: live price fetch failed for %s",
            ticker, exc_info=True,
        )

    try:
        from datetime import date as _date, timedelta as _td
        end = _date.today()
        start = end - _td(days=10)  # cover holidays / long weekends
        df = yf_adapter.get_price_history(ticker, start, end, "1d")
        if df is not None and len(df) > 0:
            import pandas as pd
            close_col = df["Close"]
            # yfinance 1.3 returns MultiIndex columns even for a single ticker,
            # so df["Close"] can be a DataFrame; pandas 3 also removed
            # float(Series). Reduce to a scalar before float() (F-7).
            if isinstance(close_col, pd.DataFrame):
                close_col = close_col.iloc[:, 0]
            close = float(close_col.iloc[-1])
            if close > 0:
                return close
    except Exception:
        logger.debug(
            "auto_paper_trade: history fallback failed for %s",
            ticker, exc_info=True,
        )

    return None


def _within_market_hours(now_et: datetime) -> bool:
    """
    True if `now_et` (US/Eastern) falls within NYSE regular trading hours
    (09:30–16:00 ET) on a full-session day.

    Auto paper trades fill at the current market price; opening one outside RTH
    would record a fill at a stale price the operator could never have achieved,
    biasing paper expectancy (decision #5). The scheduled scans run at 09:35 /
    12:00 / 15:30 ET — all comfortably inside this window — so only genuinely
    off-hours catch-up runs (e.g. a misfire after the machine wakes) are skipped.
    """
    from execution.scan_scheduler import is_market_open, regular_market_close_time
    if not is_market_open(now_et):
        return False
    # Respect early-close (half) days: the session ends at 1pm ET, so a 15:30
    # catch-up scan must not open paper trades at stale post-close prices (F-26).
    return _time(9, 30) <= now_et.time() <= regular_market_close_time(now_et)


_VALID_ENGINE_MODES = {"off", "shadow", "enforce"}


def _resolve_full_engine_mode(config) -> str:
    """Return the validated risk.full_engine_mode ('off' on anything unknown)."""
    mode = getattr(getattr(config, "risk", None), "full_engine_mode", "off")
    if mode not in _VALID_ENGINE_MODES:
        logger.warning(
            "auto_paper_trade: invalid full_engine_mode=%r — treating as 'off'", mode
        )
        return "off"
    return mode


def _build_existing_position_contexts(session, open_trades, yf_adapter=None):
    """
    Build PositionContext objects for open trades.

    Sector key is each ticker's latest signal_log.sector_etf. When a yf_adapter
    is supplied, sub_industry (yfinance 'industry') and 60-day returns are
    enriched so the sub-industry and correlation-cluster constraints are active
    (#18 follow-on); both degrade to None on failure (constraint stays inactive).
    """
    from models import SignalLog
    from risk.portfolio_constraints import PositionContext
    from risk.position_inputs import fetch_position_risk_inputs

    contexts = []
    for t in open_trades:
        row = (
            session.query(SignalLog.sector_etf)
            .filter(SignalLog.ticker == t.ticker, SignalLog.sector_etf.isnot(None))
            .order_by(SignalLog.scan_timestamp.desc())
            .first()
        )
        sub_industry, returns_60d = fetch_position_risk_inputs(yf_adapter, t.ticker)
        contexts.append(PositionContext(
            ticker=t.ticker,
            direction=t.direction,
            market_value=(t.shares or 0.0) * (t.entry_price or 0.0),
            sector_gics=row[0] if row else None,
            sub_industry=sub_industry,
            returns_60d=returns_60d,
        ))
    return contexts


def auto_paper_trade_candidates(
    session_scope: Callable,
    since: datetime,
    config,
    yf_adapter=None,
    now_et: Optional[datetime] = None,
    risk_engine=None,
    regime=None,
) -> dict:
    """
    Auto-open paper trades for all qualifying candidates written since `since`.

    Called automatically after each scan in Phase 1 auto-mode. No operator
    review — every signal that passes the Phase 1 filters is paper-traded.

    Entry price: prior day's closing price fetched via yf_adapter (injected).
    Candidates where the close cannot be fetched or atr_20 is missing are skipped.

    Sizing uses config.execution.account_equity and config.risk.phase1_risk_per_trade.

    Filters applied:
      - off-hours guard: the whole batch is skipped if `now_et` is outside RTH
        (decision #5) — paper fills only at live prices
      - scan_timestamp >= since
      - composite_score >= config.execution.auto_paper_trade_min_score
      - direction_signal in ('bullish', 'bearish')
      - earnings_proximity_tag != 'excluded'
      - corporate_action_flag = False
      - history_status = 'full'
      - was_traded = False
      - ticker dedup: no second open position in a ticker already held (no
        pyramiding) — decision #2 / F-3
      - concurrency cap: stop opening once open positions reach
        config.risk.max_concurrent_positions (candidates are taken best-first)

    Args:
        now_et: current US/Eastern datetime; defaults to now. Injectable for tests.

    Returns:
        {candidates, opened, skipped, errors, capped}
    """
    from models import SignalLog, Trade

    # Decision #5: never auto-open paper trades outside regular trading hours —
    # the entry price would be stale and the paper fill unrealisable.
    now_et = now_et or datetime.now(_ET)
    if not _within_market_hours(now_et):
        logger.info(
            "auto_paper_trade: skipped — outside market hours (%s ET)",
            now_et.strftime("%Y-%m-%d %H:%M"),
        )
        return {"candidates": 0, "opened": 0, "skipped": 0, "errors": 0,
                "capped": 0, "off_hours": True}

    min_score = getattr(config.execution, "auto_paper_trade_min_score", 0.70)
    account_equity = getattr(config.execution, "account_equity", 10_000.0)
    max_concurrent = getattr(config.risk, "max_concurrent_positions", 5)

    with session_scope() as session:
        rows = (
            session.query(SignalLog)
            .filter(
                SignalLog.scan_timestamp >= since,
                SignalLog.composite_score >= min_score,
                SignalLog.direction_signal.in_(["bullish", "bearish"]),
                SignalLog.earnings_proximity_tag != "excluded",
                SignalLog.corporate_action_flag.is_(False),
                SignalLog.history_status == "full",
                SignalLog.was_traded.is_(False),
            )
            .order_by(SignalLog.composite_score.desc())
            .all()
        )
        for r in rows:
            session.expunge(r)

    # Full risk-engine rollout (#18d): off = minimal dedup+cap (below);
    # shadow = also run approve_new_position and LOG decisions without changing
    # behavior; enforce = the engine gates which trades open and supplies sizing.
    mode = _resolve_full_engine_mode(config)
    use_engine = mode in ("shadow", "enforce") and risk_engine is not None
    if mode in ("shadow", "enforce") and risk_engine is None:
        logger.warning(
            "auto_paper_trade: full_engine_mode=%s but no risk_engine supplied — "
            "falling back to minimal cap", mode,
        )
        mode = "off"
    enforce = mode == "enforce"
    if use_engine and regime is None:
        from risk.risk_engine import RegimeLevel
        regime = RegimeLevel.NORMAL
        logger.info("auto_paper_trade: no regime supplied — assuming NORMAL")

    # Snapshot currently-open positions for the dedup + concurrency cap.
    with session_scope() as session:
        open_trades = session.query(Trade).filter(Trade.status == "open").all()
        open_tickers = {t.ticker for t in open_trades}
        open_count = len(open_trades)
        existing_ctx = (
            _build_existing_position_contexts(session, open_trades, yf_adapter)
            if use_engine else []
        )

    candidates = len(rows)
    opened = skipped = errors = capped = 0
    engine_rejected = 0

    for row in rows:
        # Concurrency cap: rows are best-first, so once we're full, stop.
        if open_count >= max_concurrent:
            capped = len(rows) - (opened + skipped + errors)
            logger.info(
                "auto_paper_trade: max_concurrent_positions=%d reached — "
                "%d lower-ranked candidate(s) not opened",
                max_concurrent, capped,
            )
            break

        # Dedup: never pyramid into a ticker we already hold (covers both
        # pre-existing open trades and earlier opens within this same batch).
        if row.ticker in open_tickers:
            logger.info(
                "auto_paper_trade: skip %s signal_id=%d reason=ticker_already_open",
                row.ticker, row.id,
            )
            skipped += 1
            continue

        if row.atr_20 is None or row.atr_20 <= 0:
            logger.info(
                "auto_paper_trade: skip %s signal_id=%d reason=atr_20_missing",
                row.ticker, row.id,
            )
            skipped += 1
            continue

        # Resolve entry price: use current market price (most accurate at scan time).
        # Falls back to prior_day_vwap from DB if yf_adapter unavailable.
        if yf_adapter is not None:
            entry_price = _fetch_entry_price(yf_adapter, row.ticker)
            if entry_price is None:
                logger.info(
                    "auto_paper_trade: skip %s signal_id=%d reason=price_fetch_failed",
                    row.ticker, row.id,
                )
                skipped += 1
                continue
        elif row.prior_day_vwap is not None and row.prior_day_vwap > 0:
            entry_price = row.prior_day_vwap
        else:
            logger.info(
                "auto_paper_trade: skip %s signal_id=%d reason=no_price_source",
                row.ticker, row.id,
            )
            skipped += 1
            continue

        direction = "long" if row.direction_signal == "bullish" else "short"

        # Full risk-engine gate (#18d). In shadow we log the decision but still
        # open via the normal path; in enforce a reject skips the candidate and
        # an approve opens with the engine's regime/CB-adjusted sizing (risk_spec).
        risk_spec = None
        if use_engine:
            from risk.position_inputs import fetch_position_risk_inputs
            cand_sub_industry, cand_returns_60d = fetch_position_risk_inputs(
                yf_adapter, row.ticker
            )
            approved, spec, reason = risk_engine.approve_new_position(
                ticker=row.ticker,
                direction=direction,
                entry_price=entry_price,
                atr_20=row.atr_20,
                account_equity=account_equity,
                catalyst_flag=row.catalyst_flag,
                regime=regime,
                existing_positions=existing_ctx,
                sector_gics=row.sector_etf,
                sub_industry=cand_sub_industry,
                returns_60d=cand_returns_60d,
                today=now_et.date(),
            )
            if enforce:
                if not approved:
                    logger.info(
                        "auto_paper_trade: ENFORCE reject %s signal_id=%d — %s",
                        row.ticker, row.id, reason,
                    )
                    engine_rejected += 1
                    continue
                risk_spec = spec
            else:  # shadow — observe only, do not change what opens
                logger.info(
                    "auto_paper_trade: SHADOW %s signal_id=%d would_%s — %s",
                    row.ticker, row.id,
                    "approve" if approved else "reject", reason,
                )
                if not approved:
                    engine_rejected += 1

        try:
            trade = open_paper_trade(
                session_scope=session_scope,
                signal_id=row.id,
                entry_price=entry_price,
                entry_time=row.scan_timestamp,
                direction=direction,
                account_equity=account_equity,
                atr_20=row.atr_20,
                catalyst_flag=row.catalyst_flag,
                config=config,
                risk_spec=risk_spec,
            )
            opened += 1
            open_count += 1
            open_tickers.add(row.ticker)
            if use_engine and trade is not None:
                # Keep the within-batch book current so later candidates see this
                # fill in the sector-capital / sub-industry / correlation /
                # overnight checks (reuse the candidate's already-fetched inputs).
                from risk.portfolio_constraints import PositionContext
                existing_ctx.append(PositionContext(
                    ticker=trade.ticker,
                    direction=trade.direction,
                    market_value=(trade.shares or 0.0) * (trade.entry_price or 0.0),
                    sector_gics=row.sector_etf,
                    sub_industry=cand_sub_industry,
                    returns_60d=cand_returns_60d,
                ))
        except ValueError as exc:
            logger.warning(
                "auto_paper_trade: skip %s signal_id=%d — %s",
                row.ticker, row.id, exc,
            )
            skipped += 1
        except Exception:
            logger.error(
                "auto_paper_trade: error for %s signal_id=%d",
                row.ticker, row.id, exc_info=True,
            )
            errors += 1

    logger.info(
        "auto_paper_trade: mode=%s candidates=%d opened=%d skipped=%d capped=%d "
        "errors=%d engine_rejected=%d",
        mode, candidates, opened, skipped, capped, errors, engine_rejected,
    )
    return {"candidates": candidates, "opened": opened, "skipped": skipped,
            "errors": errors, "capped": capped, "mode": mode,
            "engine_rejected": engine_rejected}


def feed_circuit_breakers_after_close(
    session_scope: Callable,
    cb_manager,
    account_equity: float,
    since: datetime,
    today: date,
) -> dict:
    """
    Feed realised P&L into the circuit breakers after the daily trade check (#18e).

    For every trade that closed since ``since`` (oldest first), call
    ``record_trade`` so CB1's consecutive-loss streak advances; then call
    ``record_day`` once with the day's total realised P&L and the running
    realised equity (``account_equity`` + cumulative realised P&L of all closed
    trades — a paper-Phase-1 proxy, no open-position mark-to-market). Persists
    the CB state so streaks/drawdown/halts survive a restart.

    Returns a small summary dict for logging.
    """
    from sqlalchemy import func
    from models import Trade

    with session_scope() as session:
        closed_today = (
            session.query(Trade)
            .filter(
                Trade.status == "closed",
                Trade.exit_time.isnot(None),
                Trade.exit_time >= since,
                Trade.realized_pnl.isnot(None),
            )
            .order_by(Trade.exit_time.asc())
            .all()
        )
        day_pnls = [float(t.realized_pnl) for t in closed_today]
        total_realized = float(
            session.query(func.coalesce(func.sum(Trade.realized_pnl), 0.0))
            .filter(Trade.status == "closed", Trade.realized_pnl.isnot(None))
            .scalar() or 0.0
        )

    for pnl in day_pnls:
        cb_manager.record_trade(pnl)

    day_pnl = sum(day_pnls)
    equity = account_equity + total_realized
    cb_manager.record_day(day_pnl=day_pnl, equity=equity, today=today)
    cb_manager.persist(session_scope)

    logger.info(
        "circuit_breakers: fed %d close(s) day_pnl=%.2f equity=%.2f — %s",
        len(day_pnls), day_pnl, equity, cb_manager.status(today=today),
    )
    return {"closed_today": len(day_pnls), "day_pnl": day_pnl, "equity": equity}


# ---------------------------------------------------------------------------
# Reserved for Phase 2+ live order submission — do not call in Phase 1
# ---------------------------------------------------------------------------

def _assert_paper_trading_disabled(config) -> None:
    """
    Hard stop: raise if paper_trading is still enabled in config.

    Call this at the top of any function that would submit a real order.
    Prevents live order placement until the flag is explicitly set to false.
    """
    if getattr(config.execution, "paper_trading", True):
        raise RuntimeError(
            "Order blocked: config.execution.paper_trading is true. "
            "Set paper_trading: false in arconian_config.yaml only when "
            "you are ready to trade a live account."
        )
