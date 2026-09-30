"""
Dead-Man's Switch — graduated escalation on scan heartbeat failure.

Monitors scan liveness via a heartbeat() call after each successful scan.
If no heartbeat arrives within scan_timeout_minutes, escalation begins:

  T + 0  min : Log a critical alert; attempt re-authentication (stub in Phase 0).
  T + widen  : If still no heartbeat → widen all open-position stops by 1 ATR.
  T + force  : If still no heartbeat → force-close all open positions and halt.

Timing offsets (widen_stops_minutes, force_close_minutes) are read from
DeadmanConfig. Default whitepaper values: widen=10, force=30.

action_mode (CRITICAL for paper trading):
  - "alert_only" (default): every escalation stage sends a CRITICAL alert but
    performs NO database mutation. Stops are never widened and trades are never
    force-closed. This is the correct mode for Phase 1 paper trading: there is
    no live broker order to cancel, so the only effect of stage 2/3 DB writes
    is to corrupt the trade record. A mis-tuned timeout writing NULL-P&L
    force-closes is exactly what destroyed the 2026-05 paper-trade dataset.
  - "full": stages 2/3 mutate the trades table. Reserve for Phase 4+ when the
    switch is guarding live capital alongside a real broker integration.

Other notes:
  - Re-authentication is a no-op stub (no live broker in Phase 0/1).
  - A force-close escalation sets a STICKY halt: heartbeat() does not clear it;
    only an explicit reset() does (otherwise a halt silently lifts itself on the
    next scan).
  - Telegram alerting is logged at CRITICAL and sent if TELEGRAM_BOT_TOKEN
    and TELEGRAM_CHAT_ID env vars are set.

Whitepaper reference: Section 5.4 (Dead-Man's Switch)
"""

import logging
import os
import threading
from datetime import datetime
from timeutils import utcnow
from typing import Optional

logger = logging.getLogger(__name__)


class DeadmanSwitch:
    """
    Liveness watchdog for the scan scheduler.

    After each successful scan the scheduler calls heartbeat(). If no
    heartbeat arrives within timeout_minutes, the escalation sequence runs
    on a background daemon thread.

    Args:
        timeout_minutes: Minutes before the first escalation (scan_timeout_minutes).
        widen_stops_minutes: Offset from timeout at which stops are widened.
        force_close_minutes: Offset from timeout at which positions are force-closed.
        db_session_factory: Callable returning a SQLAlchemy session context manager.
            Used to read and update open trades. None → DB operations are skipped.
        enabled: Set False to disable the watchdog entirely (useful in tests).
    """

    def __init__(
        self,
        timeout_minutes: int = 60,
        widen_stops_minutes: int = 10,
        force_close_minutes: int = 30,
        db_session_factory=None,
        enabled: bool = True,
        action_mode: str = "alert_only",
    ) -> None:
        self._timeout_secs = timeout_minutes * 60
        self._widen_secs = (timeout_minutes + widen_stops_minutes) * 60
        self._force_secs = (timeout_minutes + force_close_minutes) * 60
        self._db = db_session_factory
        self._enabled = enabled
        # "alert_only" (default) → stages 2/3 alert but never mutate the DB.
        # "full" → stages 2/3 widen stops / force-close (Phase 4+ live capital).
        self._action_mode = action_mode

        self._last_heartbeat: Optional[datetime] = None
        self._halted: bool = False
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._ever_armed: bool = False

        # Do NOT arm at init — arm when the first scan begins via ensure_armed().
        # This prevents false alarms when the system starts mid-day and the next
        # scheduled scan is more than timeout_minutes away.

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def heartbeat(self) -> None:
        """
        Record a successful scan completion and reset the watchdog timer.

        Call this after every scan that completes without error.
        """
        with self._lock:
            self._last_heartbeat = utcnow()
            # NOTE: a heartbeat does NOT clear _halted. Once the force-close
            # escalation has fired, the halt is sticky until an operator calls
            # reset() — otherwise a halt would silently lift itself on the next
            # scan (a contributor to the repeated 2026-05 escalations).
            self._reset_timer()
        logger.debug("DeadmanSwitch: heartbeat received at %s", self._last_heartbeat)

    def ensure_armed(self) -> None:
        """
        Arm the watchdog if it has not been armed yet.

        Call this when a scan is about to begin on a trading day. Idempotent —
        safe to call before every scan; only the first call has any effect.
        """
        if not self._enabled:
            return
        with self._lock:
            if not self._ever_armed:
                self._ever_armed = True
                self._arm()
                logger.info(
                    "DeadmanSwitch: armed on first scan — escalation in %d min",
                    self._timeout_secs // 60,
                )

    def halt(self) -> None:
        """Disable the watchdog (e.g. during intentional shutdown)."""
        with self._lock:
            self._cancel_timer()
            self._enabled = False
        logger.info("DeadmanSwitch: halted")

    def reset(self) -> None:
        """
        Clear a sticky halt and re-arm the watchdog.

        Call this when an operator has acknowledged a force-close escalation and
        wants monitoring to resume. heartbeat() deliberately does NOT do this
        (see its note), so a stale heartbeat cannot silently un-halt the system.
        """
        with self._lock:
            self._halted = False
            self._last_heartbeat = utcnow()
            if self._enabled:
                self._reset_timer()
        logger.info("DeadmanSwitch: reset — halt cleared, watchdog re-armed")

    @property
    def is_halted(self) -> bool:
        """True if the force-close escalation has fired."""
        return self._halted

    @property
    def last_heartbeat(self) -> Optional[datetime]:
        return self._last_heartbeat

    # ------------------------------------------------------------------
    # Internal timer management
    # ------------------------------------------------------------------

    def _arm(self) -> None:
        """Start the watchdog timer from scratch."""
        self._cancel_timer()
        self._timer = threading.Timer(self._timeout_secs, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()
        logger.debug(
            "DeadmanSwitch: armed — escalation in %d s", self._timeout_secs
        )

    def _reset_timer(self) -> None:
        """Cancel existing timer and restart from timeout_secs."""
        self._cancel_timer()
        if self._enabled:
            self._timer = threading.Timer(self._timeout_secs, self._on_timeout)
            self._timer.daemon = True
            self._timer.start()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    # ------------------------------------------------------------------
    # Escalation sequence
    # ------------------------------------------------------------------

    def _on_timeout(self) -> None:
        """
        Called on a daemon thread when the watchdog fires.

        Runs all three escalation stages sequentially, with a sleep
        between stages to match the configured offsets.
        """
        import time

        logger.critical(
            "DeadmanSwitch: TIMEOUT — no heartbeat in %d minutes. "
            "Starting escalation sequence.",
            self._timeout_secs // 60,
        )
        self._alert("ARCONIAN DEADMAN: scan timeout — no heartbeat received")

        # Stage 1: re-authentication attempt (no-op in Phase 0)
        self._attempt_reauth()

        # Stage 2: widen stops (after widen_stops_minutes from timeout)
        widen_delay = self._widen_secs - self._timeout_secs
        if widen_delay > 0:
            time.sleep(widen_delay)
        with self._lock:
            # Check if heartbeat arrived during sleep
            if self._last_heartbeat and (
                (utcnow() - self._last_heartbeat).total_seconds() < self._timeout_secs
            ):
                logger.info("DeadmanSwitch: heartbeat received during widen delay — aborting")
                return
        self._widen_stops()
        self._alert("ARCONIAN DEADMAN: stops widened by 1 ATR — operator action required")

        # Stage 3: force-close (after force_close_minutes from timeout)
        force_delay = self._force_secs - self._widen_secs
        if force_delay > 0:
            time.sleep(force_delay)
        with self._lock:
            if self._last_heartbeat and (
                (utcnow() - self._last_heartbeat).total_seconds() < self._timeout_secs
            ):
                logger.info("DeadmanSwitch: heartbeat received during force delay — aborting")
                return
        self._force_close_all()
        with self._lock:
            self._halted = True
        self._alert("ARCONIAN DEADMAN: all positions force-closed. System HALTED.")

    def _attempt_reauth(self) -> None:
        """
        Stage 1: attempt broker re-authentication.

        Phase 0 stub — logs only. Phase 2+ should call the Schwab adapter's
        token-refresh endpoint here.
        """
        logger.warning("DeadmanSwitch: Stage 1 — re-authentication attempt (Phase 0 stub)")

    def _widen_stops(self) -> None:
        """
        Stage 2: Widen stop_price on all open trades by 1 ATR.

        In Phase 0 the trades table is empty so this is a safe no-op.
        Phase 2+ should also update live orders via the order manager.
        """
        logger.warning("DeadmanSwitch: Stage 2 — widening stops by 1 ATR")
        if self._action_mode != "full":
            logger.warning(
                "DeadmanSwitch: action_mode=%r — alert only, NOT widening stops "
                "(no DB mutation in paper mode)", self._action_mode,
            )
            return
        if self._db is None:
            logger.warning("DeadmanSwitch: no db_session_factory — skipping stop widen")
            return

        try:
            from models import Trade
            with self._db() as session:
                open_trades = session.query(Trade).filter(Trade.status == "open").all()
                widened = 0
                for trade in open_trades:
                    if trade.stop_price is None or trade.atr_at_entry is None:
                        continue
                    if trade.direction == "long":
                        trade.stop_price -= trade.atr_at_entry
                    else:
                        trade.stop_price += trade.atr_at_entry
                    widened += 1
                session.commit()
            logger.warning("DeadmanSwitch: widened stops on %d open trade(s)", widened)
        except Exception:
            logger.exception("DeadmanSwitch: failed to widen stops")

    def _force_close_all(self) -> None:
        """
        Stage 3: Mark all open trades as force-closed.

        Phase 0: writes exit_reason='force_close' and status='closed' to DB.
        Phase 2+: also submit market orders to broker before marking closed.
        """
        logger.critical("DeadmanSwitch: Stage 3 — FORCE CLOSING all open positions")
        if self._action_mode != "full":
            logger.critical(
                "DeadmanSwitch: action_mode=%r — alert only, NOT force-closing "
                "trades (no DB mutation in paper mode)", self._action_mode,
            )
            return
        if self._db is None:
            logger.critical("DeadmanSwitch: no db_session_factory — skipping force close")
            return

        try:
            from models import Trade
            now = utcnow()
            with self._db() as session:
                open_trades = session.query(Trade).filter(Trade.status == "open").all()
                closed = 0
                for trade in open_trades:
                    trade.status = "closed"
                    trade.exit_reason = "force_close"
                    trade.exit_time = now
                    closed += 1
                session.commit()
            logger.critical(
                "DeadmanSwitch: force-closed %d trade(s) at %s", closed, now
            )
        except Exception:
            logger.exception("DeadmanSwitch: failed to force-close trades")

    def _alert(self, message: str) -> None:
        """
        Send an operator alert. Logs at CRITICAL level always.

        If TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables are
        set, attempts to send a Telegram message via the Bot API. Failures
        are logged but do not raise, to avoid interrupting the escalation.
        """
        logger.critical("ALERT: %s", message)

        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not (token and chat_id):
            return

        try:
            import urllib.request
            import urllib.parse
            import json

            payload = json.dumps({"chat_id": chat_id, "text": message}).encode()
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning("DeadmanSwitch: Telegram returned status %d", resp.status)
        except Exception:
            logger.warning("DeadmanSwitch: Telegram alert failed", exc_info=True)
