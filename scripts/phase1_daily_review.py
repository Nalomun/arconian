"""
Phase 1 daily candidate review CLI.

Shows tradeable candidates from the most recent scan, lets the operator
review each one, and records paper trades.

Usage:
  python scripts/phase1_daily_review.py --equity 25000
  python scripts/phase1_daily_review.py --equity 25000 --min-score 0.80

Suggested entry is the closing price for the scan date fetched from yfinance.
This is a simplification — Phase 2 will use the actual fill price at signal time.
"""

import argparse
import sys
from datetime import datetime, timedelta
from timeutils import utcnow
from pathlib import Path

# Project root on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _get_candidates(session_scope, min_score: float) -> list:
    """
    Return signal_log rows from the most recent scan that pass all Phase 1 filters.

    Filters:
      - composite_score >= min_score
      - direction_signal in ('bullish', 'bearish')
      - earnings_proximity_tag != 'excluded'
      - corporate_action_flag = False
      - history_status = 'full'
      - was_traded = False
    """
    from models import SignalLog
    from sqlalchemy import func

    with session_scope() as session:
        # Find the most recent scan_timestamp
        latest_ts = session.query(func.max(SignalLog.scan_timestamp)).scalar()
        if latest_ts is None:
            return []

        # Window covers the most recent scan run (up to 2 hours wide for safety)
        window_start = latest_ts - timedelta(hours=2)

        rows = (
            session.query(SignalLog)
            .filter(
                SignalLog.scan_timestamp >= window_start,
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
    return rows


def _get_suggested_entry(yf_adapter, ticker: str, scan_date) -> float | None:
    """
    Fetch the closing price for scan_date from yfinance.
    Returns None if unavailable.

    NOTE: Phase 1 simplification — uses EOD close as entry proxy.
    Phase 2 will capture the actual fill price at signal time.
    """
    try:
        from datetime import timedelta
        start = scan_date.strftime("%Y-%m-%d")
        end = (scan_date + timedelta(days=2)).strftime("%Y-%m-%d")
        hist = yf_adapter.get_price_history(
            ticker=ticker, start=start, end=end, interval="1d"
        )
        if hist is None or hist.empty:
            return None
        if hasattr(hist.columns, "levels"):
            close_col = ("Close", ticker)
            if close_col in hist.columns:
                vals = hist[close_col].dropna()
            else:
                return None
        else:
            vals = hist["Close"].dropna()
        if vals.empty:
            return None
        return float(vals.iloc[0])
    except Exception:
        return None


def _format_candidate(row, suggested_entry: float | None) -> str:
    """Format a single candidate for display."""
    direction = "LONG" if row.direction_signal == "bullish" else "SHORT"
    entry_str = f"${suggested_entry:.2f}" if suggested_entry is not None else "N/A"

    opt_str = f"{row.options_composite:.2f}" if row.options_composite is not None else "N/A"
    lines = [
        f"  Ticker:    {row.ticker}  |  {direction}  |  score={row.composite_score:.3f}",
        f"  Signals:   vol={row.volume_pctile_20d or 0:.2f}  "
        f"ret={row.return_pctile_60d or 0:.2f}  "
        f"opt={opt_str}  "
        f"sec_rs={row.sector_rs_percentile or 0:.2f}  "
        f"delay={row.delay_score or 0:.2f}",
        f"  Context:   catalyst={row.catalyst_flag}  "
        f"mktcap=${row.market_cap_mm or 0:.0f}M  "
        f"earn_days={row.days_to_next_earnings}",
        f"  Prices:    vwap_prev={row.prior_day_vwap or 0:.2f}  "
        f"atr20={row.atr_20 or 0:.3f}  "
        f"suggested_entry={entry_str}",
    ]
    return "\n".join(lines)


def _prompt_entry_price(suggested: float | None) -> float | None:
    """Prompt operator for entry price, defaulting to suggested."""
    default_str = f" [{suggested:.2f}]" if suggested is not None else ""
    while True:
        raw = input(f"  Entry price{default_str}: ").strip()
        if raw == "" and suggested is not None:
            return suggested
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
        print("  Invalid price. Enter a positive number or press Enter to use default.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Arconian Phase 1 daily candidate review")
    parser.add_argument(
        "--equity",
        type=float,
        required=True,
        help="Account equity in USD (required for position sizing)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.70,
        dest="min_score",
        help="Minimum composite score to show (default: 0.70)",
    )
    parser.add_argument(
        "--config",
        default="config/arconian_config.yaml",
        help="Path to arconian_config.yaml",
    )
    args = parser.parse_args()

    # Initialize config
    try:
        from config.config_loader import ConfigLoader
        config = ConfigLoader(args.config)
    except Exception as exc:
        print(f"ERROR: Could not load config: {exc}")
        return 1

    # Initialize DB
    try:
        from db import init_db, session_scope
        init_db()
    except Exception as exc:
        print(f"ERROR: Could not initialise database: {exc}")
        return 1

    # Adapters
    from data.yfinance_adapter import YFinanceAdapter
    yf = YFinanceAdapter()

    from execution.order_manager import open_paper_trade

    candidates = _get_candidates(session_scope, args.min_score)

    if not candidates:
        print(f"No candidates found (min_score={args.min_score:.2f}).")
        return 0

    scan_ts = candidates[0].scan_timestamp
    print(f"\nPhase 1 Daily Review — {scan_ts.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Candidates: {len(candidates)}  |  equity: ${args.equity:,.0f}  |  min_score: {args.min_score:.2f}\n")
    print("=" * 60)

    taken = 0
    for idx, row in enumerate(candidates):
        scan_date = row.scan_timestamp.date()
        suggested = _get_suggested_entry(yf, row.ticker, scan_date)

        print(f"\n[{idx + 1}/{len(candidates)}]")
        print(_format_candidate(row, suggested))
        print()

        while True:
            choice = input("  [t]ake  [s]kip  [q]uit: ").strip().lower()
            if choice in ("t", "take"):
                if row.atr_20 is None or row.atr_20 <= 0:
                    print("  Cannot open trade: atr_20 is missing for this signal.")
                    break
                entry_price = _prompt_entry_price(suggested)
                if entry_price is None:
                    print("  Entry price required.")
                    break
                try:
                    trade = open_paper_trade(
                        session_scope=session_scope,
                        signal_id=row.id,
                        entry_price=entry_price,
                        entry_time=utcnow(),
                        direction="long" if row.direction_signal == "bullish" else "short",
                        account_equity=args.equity,
                        atr_20=row.atr_20,
                        catalyst_flag=row.catalyst_flag,
                        config=config,
                    )
                    taken += 1
                    print(
                        f"\n  ✓ Paper trade opened — id={trade.id} | "
                        f"stop=${trade.stop_price:.2f} | "
                        f"target=${trade.target_price:.2f} | "
                        f"{int(trade.shares)} shares | "
                        f"signal_id={row.id}"
                    )
                except ValueError as exc:
                    print(f"  ERROR: {exc}")
                break
            elif choice in ("s", "skip"):
                break
            elif choice in ("q", "quit"):
                print(f"\nReview ended early. Taken: {taken}/{idx + 1} reviewed.")
                return 0
            else:
                print("  Please enter t, s, or q.")

    print(f"\n{'=' * 60}")
    print(f"Review complete. Taken {taken} of {len(candidates)} candidates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
