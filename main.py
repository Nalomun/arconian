"""
Arconian — entry point.

Start sequence:
  1. Load and validate config (config/arconian_config.yaml).
  2. Initialise SQLite database (WAL mode, run migrations).
  3. Track any config parameter changes to parameter_history.
  4. Build adapters, risk engine, and signal engine.
  5. Arm the dead-man's switch.
  6. Start the scan scheduler (blocking).

Behaviour:
  Signals are scored and written to signal_log. When execution.auto_paper_trade
  is true, qualifying candidates are opened as paper trades via order_manager.
  No broker orders are ever placed (Schwab order placement is a stub).

Usage:
  python main.py
  python main.py --config path/to/config.yaml
  python main.py --dry-run   (run one morning scan immediately and exit)

Whitepaper reference: Section 5 (Execution Architecture)
"""

import argparse
import logging
import logging.config
import sys
from pathlib import Path

# Ensure the project root is on sys.path when running as a script
sys.path.insert(0, str(Path(__file__).parent))

from timeutils import utcnow

# Load .env before any adapter imports so credentials are available
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Logging — configure before any module import so startup messages appear
# ---------------------------------------------------------------------------

# Ensure logs/ exists before the file handler tries to open it
Path("logs").mkdir(parents=True, exist_ok=True)

logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "stream": "ext://sys.stdout",
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "standard",
            "filename": "logs/arconian.log",
            "maxBytes": 10_485_760,  # 10 MB
            "backupCount": 5,
            "encoding": "utf-8",
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["console", "file"],
    },
})

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Telegram helper
# ---------------------------------------------------------------------------

def _send_telegram(message: str) -> None:
    """Send a Telegram message if credentials are configured. Never raises."""
    import urllib.request
    import json

    token = __import__("os").environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = __import__("os").environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat_id):
        return
    try:
        payload = json.dumps({"chat_id": chat_id, "text": message}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        logger.warning("Telegram notification failed", exc_info=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Arconian automated trading system")
    p.add_argument(
        "--config",
        default="config/arconian_config.yaml",
        help="Path to arconian_config.yaml (default: config/arconian_config.yaml)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run one morning scan immediately and exit without starting the scheduler",
    )
    p.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override root log level",
    )
    return p.parse_args()


def _ensure_dirs() -> None:
    """Create runtime directories that must exist before logging starts."""
    for d in ("logs", "data"):
        Path(d).mkdir(parents=True, exist_ok=True)


def main() -> int:
    _ensure_dirs()
    args = _parse_args()

    if args.log_level:
        logging.getLogger().setLevel(args.log_level)

    logger.info("=" * 60)
    logger.info("Arconian starting (paper trading only — no broker orders are placed)")
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # 1. Config
    # ------------------------------------------------------------------
    try:
        from config.config_loader import ConfigLoader
        config = ConfigLoader(args.config)
        logger.info("Config loaded: %s (hash: %s)", args.config, config.config_hash[:12])
    except (FileNotFoundError, ValueError) as exc:
        logger.critical("Config load failed: %s", exc)
        return 1

    # ------------------------------------------------------------------
    # 2. Database
    # ------------------------------------------------------------------
    try:
        from db import init_db, session_scope
        init_db()
        logger.info("Database initialised (WAL mode)")
    except Exception:
        logger.critical("Database initialisation failed", exc_info=True)
        return 1

    # ------------------------------------------------------------------
    # 3. Parameter history (track config changes)
    # ------------------------------------------------------------------
    try:
        from journal.parameter_tracker import record_config_snapshot
        record_config_snapshot(config, session_scope)
    except Exception:
        logger.warning("Parameter history update failed (non-fatal)", exc_info=True)

    # ------------------------------------------------------------------
    # 4. Adapters and engines
    # ------------------------------------------------------------------
    try:
        from data.schwab_adapter import SchwabAdapter
        from data.yfinance_adapter import YFinanceAdapter
        from data.edgar_adapter import EdgarAdapter as EDGARAdapter
        from data.earnings_calendar import EarningsCalendar
        from universe.universe_manager import UniverseManager
        from signals.signal_engine import SignalEngine
        from risk.circuit_breakers import CircuitBreakerManager
        from risk.risk_engine import RiskEngine

        schwab = SchwabAdapter.from_token_file()
        yfinance = YFinanceAdapter()
        edgar = EDGARAdapter()
        earnings_calendar = EarningsCalendar(yf_adapter=yfinance)

        universe_mgr = UniverseManager(
            config=config,
            yf_adapter=yfinance,
            schwab_adapter=schwab,
        )

        # Load persisted CB state so streaks/drawdown/halts survive a restart
        # (#18b/#18e). Falls back to a clean state if nothing is stored.
        cb_manager = CircuitBreakerManager.load(
            session_scope,
            config_max_positions=config.risk.max_concurrent_positions,
        )

        risk_engine = RiskEngine(config=config, cb_manager=cb_manager)

        signal_engine = SignalEngine(
            config=config,
            yf_adapter=yfinance,
            schwab_adapter=schwab,
            edgar_adapter=edgar,
            earnings_calendar=earnings_calendar,
        )

        logger.info("Adapters and engines initialised")
        _send_telegram("Arconian started — scheduler armed, awaiting scans.")
    except Exception:
        logger.critical("Engine initialisation failed", exc_info=True)
        _send_telegram("Arconian FAILED to start — check logs.")
        return 1

    # ------------------------------------------------------------------
    # 5. Dead-man's switch
    # ------------------------------------------------------------------
    from execution.deadman_switch import DeadmanSwitch

    deadman = DeadmanSwitch(
        timeout_minutes=config.deadman.scan_timeout_minutes,
        widen_stops_minutes=config.deadman.widen_stops_minutes,
        force_close_minutes=config.deadman.force_close_minutes,
        db_session_factory=session_scope,
        enabled=not args.dry_run,  # disable watchdog for dry runs
        action_mode=getattr(config.deadman, "action_mode", "alert_only"),
    )

    # ------------------------------------------------------------------
    # 6. Job callables
    # ------------------------------------------------------------------

    def _do_scan(scan_type: str) -> None:
        import datetime as _dt
        scan_start = utcnow()
        logger.info("=== Scan started: %s ===", scan_type)
        results = signal_engine.run_scan(scan_type=scan_type)
        scored = [r for r in results if r.composite_score is not None and not r.was_excluded]
        logger.info(
            "=== Scan complete: %s — %d candidates scored, %d total ===",
            scan_type, len(scored), len(results),
        )
        if scored:
            top = sorted(scored, key=lambda r: r.composite_score or 0, reverse=True)[:5]
            for r in top:
                logger.info(
                    "  %s | score=%.3f | dir=%s | vol_20d=%.2f",
                    r.ticker,
                    r.composite_score or 0,
                    r.direction_signal,
                    r.volume_pctile_20d or 0,
                )
            top5_lines = "\n".join(
                f"  {i+1}. {r.ticker} {r.composite_score:.3f} {r.direction_signal}"
                for i, r in enumerate(top)
            )
            _send_telegram(
                f"Arconian scan ({scan_type}) — {len(scored)} scored\n{top5_lines}"
            )

        # Phase 1 auto paper trading — no operator review
        if getattr(config.execution, "auto_paper_trade", False):
            try:
                from execution.order_manager import auto_paper_trade_candidates

                # Compute market regime only when the full engine is active
                # (shadow/enforce); off-mode skips the VIX/IWM fetch entirely.
                regime = None
                if getattr(config.risk, "full_engine_mode", "off") in ("shadow", "enforce"):
                    from risk.regime_inputs import fetch_regime_inputs
                    ri = fetch_regime_inputs(yfinance)
                    if ri is not None:
                        regime = risk_engine.current_regime(
                            ri.vix, ri.iwm_10d_return, ri.iwm_20d_return,
                        )

                pt = auto_paper_trade_candidates(
                    session_scope=session_scope,
                    since=scan_start,
                    config=config,
                    yf_adapter=yfinance,
                    risk_engine=risk_engine,
                    regime=regime,
                )
                if pt["opened"] > 0:
                    logger.info(
                        "auto_paper_trade: opened %d paper trades from %s scan",
                        pt["opened"], scan_type,
                    )
            except Exception:
                logger.error("auto_paper_trade: unhandled error", exc_info=True)

    def _do_universe_check() -> None:
        logger.info("=== Universe daily check ===")
        universe_mgr.run_daily_check()
        logger.info("=== Universe daily check complete ===")

    def _do_maintenance() -> None:
        logger.info("=== After-close maintenance ===")
        from journal.maintenance import run_after_close
        summary = run_after_close(
            session_scope=session_scope,
            yf_adapter=yfinance,
            config=config,
        )
        # One-line consolidated summary for ops
        oc = summary["outcome_collector"]
        cv = summary["cost_validator"]
        ic = summary["ic_tracker"]
        logger.info(
            "=== After-close maintenance complete: "
            "outcomes(processed=%s) costs(processed=%s flag=%s) ic(written=%s) ===",
            oc.get("processed", oc.get("error", "?")),
            cv.get("processed", cv.get("error", "?")),
            cv.get("flag_raised", cv.get("error", "?")),
            ic.get("written", ic.get("error", "?")),
        )

    def _do_trade_check() -> None:
        logger.info("=== Paper trade check ===")
        from execution.order_manager import (
            check_open_trades,
            feed_circuit_breakers_after_close,
        )
        import datetime as _dt
        run_start = utcnow()
        results = check_open_trades(
            session_scope=session_scope,
            yf_adapter=yfinance,
            as_of_date=_dt.date.today(),
            config=config,
        )
        logger.info(
            "=== Paper trade check complete: stop=%d trailing=%d time=%d tp1=%d errors=%d ===",
            results["closed_stop"], results["closed_trailing"],
            results["closed_time"], results.get("tp1_partial", 0), results["errors"],
        )

        # Feed realised P&L into the circuit breakers and persist (#18e). Always
        # runs so CB streaks/drawdown stay current even in off/shadow mode — the
        # breakers only *gate* trades once full_engine_mode is 'enforce'.
        try:
            feed_circuit_breakers_after_close(
                session_scope=session_scope,
                cb_manager=cb_manager,
                account_equity=getattr(config.execution, "account_equity", 10_000.0),
                since=run_start,
                today=_dt.date.today(),
            )
        except Exception:
            logger.error("circuit_breakers: feed after trade check failed", exc_info=True)

    # ------------------------------------------------------------------
    # Dry-run: run one scan and exit
    # ------------------------------------------------------------------
    if args.dry_run:
        logger.info("Dry-run mode: running morning scan once then exiting")
        try:
            _do_scan("open")
        except Exception:
            logger.exception("Dry-run scan failed")
            return 1
        deadman.halt()
        return 0

    # ------------------------------------------------------------------
    # 7. Scheduler (blocking)
    # ------------------------------------------------------------------
    from execution.scan_scheduler import ScanScheduler

    scheduler = ScanScheduler(
        scan_fn=_do_scan,
        universe_check_fn=_do_universe_check,
        maintenance_fn=_do_maintenance,
        deadman=deadman,
        scan_times_et=config.execution.scan_times_et,
        trade_check_fn=_do_trade_check,
    )

    logger.info("Starting scheduler — press Ctrl+C to stop")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped by operator")
        _send_telegram("Arconian stopped by operator.")
    except Exception:
        logger.critical("Scheduler crashed", exc_info=True)
        _send_telegram("Arconian CRASHED — check logs.")
    finally:
        deadman.halt()

    logger.info("Arconian shutdown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
