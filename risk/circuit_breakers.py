"""
Circuit breaker state tracking and enforcement.

Monitors and enforces all 4 circuit breakers defined in whitepaper Section 4.5:

  CB1 — Consecutive losses
      Trigger : 5 consecutive losing trades
      Action  : Reduce per-trade risk to 0.5× for next 10 trades
      Recovery: Automatic after 10 trades at reduced risk

  CB2 — Rolling 10-day drawdown
      Trigger : Account drawdown ≥ 7% over trailing 10 trading days
      Action  : Max concurrent positions → 3 for 15 trading days
      Recovery: Step to 4 positions for 10 days → return to 5 (time-based)

  CB3 — Rolling 20-day drawdown
      Trigger : Account drawdown ≥ 12% over trailing 20 trading days
      Action  : Halt all new position entry
      Recovery: MANUAL only — requires documented review + explicit reset

  CB4 — Single-day loss
      Trigger : Portfolio loses > 3% of account equity in a single day
      Action  : No new positions for 2 trading days
      Recovery: Automatic after 2 trading days

State is maintained in-memory and intended to be constructed at startup
from the Trades table or a dedicated checkpoint. In Phase 0 (display only),
the state resets at process restart; Phase 2+ should persist to DB.

Whitepaper reference: Section 4.5 (Circuit Breakers)
"""

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from timeutils import utcnow
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Singleton row id for the persisted CB state (Phase 1: one account).
_CB_STATE_ROW_ID = 1


def _add_trading_days(start: date, n: int) -> date:
    """
    Return the date ``n`` trading days (Mon–Fri) after ``start``.

    Uses a business-day offset so a Friday trigger blocks the next Monday and
    Tuesday instead of expiring over the weekend. The old code added ``n``
    *calendar* days, so a Friday CB4 trigger (+2 days = Sunday) imposed zero
    effective halt — trading resumed Monday as if nothing happened (F-17).
    Holidays are not modeled; weekday granularity is enough to close the
    zero-halt gap.
    """
    offset = np.busday_offset(np.datetime64(start, "D"), n, roll="forward")
    return offset.astype("datetime64[D]").astype(object)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

_CB1_LOSS_THRESHOLD = 5        # consecutive losses before trigger
_CB1_TRADES_AT_REDUCED = 10    # trades at 0.5× risk before auto-recovery
_CB2_DRAWDOWN_PCT = 0.07       # 7% rolling 10-day drawdown
_CB2_LOOKBACK_DAYS = 10
_CB2_PHASE1_DAYS = 15          # days at max 3 positions
_CB2_PHASE2_DAYS = 10          # days at max 4 positions
_CB3_DRAWDOWN_PCT = 0.12       # 12% rolling 20-day drawdown
_CB3_LOOKBACK_DAYS = 20
_CB4_LOSS_PCT = 0.03           # 3% single-day loss
_CB4_HALT_TRADING_DAYS = 2     # trading days blocked (Mon–Fri; see _add_trading_days)


# ---------------------------------------------------------------------------
# State dataclass
# ---------------------------------------------------------------------------

@dataclass
class CircuitBreakerState:
    """
    Snapshot of all circuit breaker counters.

    Intended to be serialisable for DB persistence in Phase 2+.
    """

    # CB1
    consecutive_losses: int = 0
    cb1_trades_remaining: int = 0          # > 0 means reduced-risk mode is active

    # CB2
    cb2_trigger_date: Optional[date] = None  # None = not triggered

    # CB3
    cb3_halted: bool = False               # True = manual halt active

    # CB4
    cb4_halt_until: Optional[date] = None  # None or a future date

    # Rolling equity snapshots (not serialised as-is; rebuild from equity_log)
    equity_log: list = field(default_factory=list, repr=False)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class CircuitBreakerManager:
    """
    Manages all four circuit breaker states for one trading account.

    Usage pattern:
      # After each trade settles
      manager.record_trade(pnl=realised_pnl_dollars)
      # After market close each day
      manager.record_day(day_pnl=total_day_pnl, equity=current_equity, today=date.today())
      # Before approving any new entry
      allowed, reason = manager.allow_new_positions(today=date.today())
      max_pos = manager.get_max_positions(today=date.today())
      risk_mult = manager.get_risk_multiplier()

    Args:
        config_max_positions: Configured maximum concurrent positions (normally 5).
        state: Optional pre-loaded CircuitBreakerState (for resuming after restart).
    """

    def __init__(
        self,
        config_max_positions: int = 5,
        state: Optional[CircuitBreakerState] = None,
    ) -> None:
        self._max_pos = config_max_positions
        self._state = state or CircuitBreakerState()
        # Rolling equity window (deque of (date, equity) tuples)
        self._equity_window: deque = deque(maxlen=max(_CB2_LOOKBACK_DAYS, _CB3_LOOKBACK_DAYS))
        # Rebuild window from state log if provided
        for entry in (state.equity_log if state else []):
            self._equity_window.append(entry)

    # ------------------------------------------------------------------
    # Update methods (call after each trade / after each day close)
    # ------------------------------------------------------------------

    def record_trade(self, pnl: float) -> None:
        """
        Update CB1 state after a trade settles.

        Args:
            pnl: Realised profit/loss in dollars for the completed trade.
                 Positive = win, negative = loss.
        """
        if pnl < 0:
            self._state.consecutive_losses += 1
            if self._state.consecutive_losses >= _CB1_LOSS_THRESHOLD:
                if self._state.cb1_trades_remaining == 0:
                    # Fresh trigger
                    self._state.cb1_trades_remaining = _CB1_TRADES_AT_REDUCED
                    logger.warning(
                        "CB1 triggered: %d consecutive losses → reduced risk for %d trades",
                        self._state.consecutive_losses, _CB1_TRADES_AT_REDUCED,
                    )
                else:
                    # Already in reduced-risk mode; extend by 10 more
                    self._state.cb1_trades_remaining = _CB1_TRADES_AT_REDUCED
                    logger.warning(
                        "CB1 re-triggered during reduced-risk window → reset to %d trades",
                        _CB1_TRADES_AT_REDUCED,
                    )
        else:
            self._state.consecutive_losses = 0

        # Each trade (win or loss) counts against the reduced-risk window
        if self._state.cb1_trades_remaining > 0:
            self._state.cb1_trades_remaining -= 1
            if self._state.cb1_trades_remaining == 0:
                logger.info("CB1 recovered: reduced-risk window exhausted")

    def record_day(
        self,
        day_pnl: float,
        equity: float,
        today: Optional[date] = None,
    ) -> None:
        """
        Update CB2, CB3, CB4 state after each day's close.

        Args:
            day_pnl: Total portfolio P&L for the day in dollars.
            equity:  Account equity at day close in dollars.
            today:   Trading date. Defaults to date.today().
        """
        today = today or date.today()
        self._equity_window.append((today, equity))
        self._state.equity_log = list(self._equity_window)

        # ---- CB4: single-day loss ----
        if equity > 0 and abs(day_pnl) / equity > _CB4_LOSS_PCT and day_pnl < 0:
            self._state.cb4_halt_until = _add_trading_days(today, _CB4_HALT_TRADING_DAYS)
            logger.warning(
                "CB4 triggered: day_pnl=%.1f%% of equity → no new positions until %s",
                100 * day_pnl / equity, self._state.cb4_halt_until,
            )

        # ---- CB2 and CB3: rolling drawdown checks ----
        if len(self._equity_window) >= 2:
            equities = [eq for _, eq in self._equity_window]
            peak_10 = max(equities[-min(_CB2_LOOKBACK_DAYS, len(equities)):])
            if equity > 0 and peak_10 > 0:
                drawdown_10 = (peak_10 - equity) / peak_10
                if drawdown_10 >= _CB2_DRAWDOWN_PCT and self._state.cb2_trigger_date is None:
                    self._state.cb2_trigger_date = today
                    logger.warning(
                        "CB2 triggered: 10-day drawdown=%.1f%% → max positions→3 for %d days",
                        100 * drawdown_10, _CB2_PHASE1_DAYS,
                    )

            peak_20 = max(equities[-min(_CB3_LOOKBACK_DAYS, len(equities)):])
            if equity > 0 and peak_20 > 0:
                drawdown_20 = (peak_20 - equity) / peak_20
                if drawdown_20 >= _CB3_DRAWDOWN_PCT and not self._state.cb3_halted:
                    self._state.cb3_halted = True
                    logger.critical(
                        "CB3 triggered: 20-day drawdown=%.1f%% → FULL HALT. Manual review required.",
                        100 * drawdown_20,
                    )

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def allow_new_positions(self, today: Optional[date] = None) -> tuple[bool, str]:
        """
        Returns (allowed, reason).

        Args:
            today: Current date. Defaults to date.today().
        """
        today = today or date.today()

        if self._state.cb3_halted:
            return False, "CB3_12pct_drawdown_halt (manual reset required)"

        # cb4_halt_until is the LAST blocked trading day, so block through it
        # inclusive (<=). With calendar-day arithmetic this distinction was
        # moot; with trading-day arithmetic the halt day itself must still block.
        if self._state.cb4_halt_until and today <= self._state.cb4_halt_until:
            days_left = (self._state.cb4_halt_until - today).days
            return False, f"CB4_single_day_loss ({days_left}d remaining)"

        return True, "ok"

    def get_max_positions(self, today: Optional[date] = None) -> int:
        """
        Maximum concurrent open positions based on active circuit breaker state.

        Args:
            today: Current date. Defaults to date.today().
        """
        today = today or date.today()

        if self._state.cb2_trigger_date is None:
            return self._max_pos

        days_since = (today - self._state.cb2_trigger_date).days
        if days_since < _CB2_PHASE1_DAYS:
            return 3   # Phase 1: restricted
        elif days_since < _CB2_PHASE1_DAYS + _CB2_PHASE2_DAYS:
            return 4   # Phase 2: recovering
        else:
            # Full recovery — clear the trigger
            self._state.cb2_trigger_date = None
            logger.info("CB2 fully recovered (%d days elapsed)", days_since)
            return self._max_pos

    def get_risk_multiplier(self) -> float:
        """
        Return the per-trade risk multiplier.

        Returns:
            0.5 if CB1 reduced-risk mode is active, 1.0 otherwise.
        """
        return 0.5 if self._state.cb1_trades_remaining > 0 else 1.0

    def reset_cb3(self, reason: str) -> None:
        """
        Manually clear the CB3 halt. Must be called with a documented reason.

        Args:
            reason: Free-text description of the review outcome that justifies
                    resuming trading (e.g., 'reviewed 12 losing trades: edge
                    intact, temporary regime shift, resuming with reduced size').
        """
        if not self._state.cb3_halted:
            logger.warning("reset_cb3 called but CB3 is not currently halted")
            return
        self._state.cb3_halted = False
        logger.critical("CB3 MANUALLY CLEARED by operator. Reason: %s", reason)

    # ------------------------------------------------------------------
    # Convenience summary
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Persistence (Tier-3 #18 / F-17)
    # ------------------------------------------------------------------

    @classmethod
    def load(
        cls,
        session_scope: Callable,
        config_max_positions: int = 5,
    ) -> "CircuitBreakerManager":
        """
        Construct a manager from the persisted singleton CB state, or a clean
        one if nothing is stored. Streaks/drawdown/halts survive restarts.
        """
        return cls(
            config_max_positions=config_max_positions,
            state=load_cb_state(session_scope),
        )

    def get_state(self) -> CircuitBreakerState:
        """Return the live state (equity_log is kept in sync by record_day)."""
        return self._state

    def persist(self, session_scope: Callable) -> None:
        """Write the current state to the singleton row. Call after updates."""
        save_cb_state(session_scope, self._state)

    def status(self, today: Optional[date] = None) -> dict:
        """Return a human-readable status dict for logging."""
        today = today or date.today()
        allowed, reason = self.allow_new_positions(today)
        return {
            "allowed": allowed,
            "reason": reason,
            "max_positions": self.get_max_positions(today),
            "risk_multiplier": self.get_risk_multiplier(),
            "cb1_consecutive_losses": self._state.consecutive_losses,
            "cb1_trades_remaining": self._state.cb1_trades_remaining,
            "cb2_trigger_date": str(self._state.cb2_trigger_date) if self._state.cb2_trigger_date else None,
            "cb3_halted": self._state.cb3_halted,
            "cb4_halt_until": str(self._state.cb4_halt_until) if self._state.cb4_halt_until else None,
        }


# ---------------------------------------------------------------------------
# State persistence helpers (singleton row)
# ---------------------------------------------------------------------------

def load_cb_state(session_scope: Callable) -> Optional[CircuitBreakerState]:
    """
    Load the persisted CircuitBreakerState, or None if no row exists.

    Returns None (clean state) on any read/parse error rather than raising —
    a corrupt CB row must not block startup.
    """
    from models import CircuitBreakerStateRow

    try:
        with session_scope() as session:
            row = session.get(CircuitBreakerStateRow, _CB_STATE_ROW_ID)
            if row is None:
                return None
            equity_log: list = []
            if row.equity_log_json:
                for d_str, eq in json.loads(row.equity_log_json):
                    equity_log.append((date.fromisoformat(d_str), float(eq)))
            return CircuitBreakerState(
                consecutive_losses=row.consecutive_losses,
                cb1_trades_remaining=row.cb1_trades_remaining,
                cb2_trigger_date=row.cb2_trigger_date,
                cb3_halted=row.cb3_halted,
                cb4_halt_until=row.cb4_halt_until,
                equity_log=equity_log,
            )
    except Exception:
        logger.warning("load_cb_state failed — starting from a clean CB state", exc_info=True)
        return None


def save_cb_state(session_scope: Callable, state: CircuitBreakerState) -> None:
    """Upsert the singleton CB-state row. Never raises to the caller."""
    from models import CircuitBreakerStateRow

    try:
        equity_log_json = json.dumps(
            [[d.isoformat(), eq] for d, eq in (state.equity_log or [])]
        )
        with session_scope() as session:
            row = session.get(CircuitBreakerStateRow, _CB_STATE_ROW_ID)
            if row is None:
                row = CircuitBreakerStateRow(id=_CB_STATE_ROW_ID)
                session.add(row)
            row.consecutive_losses = state.consecutive_losses
            row.cb1_trades_remaining = state.cb1_trades_remaining
            row.cb2_trigger_date = state.cb2_trigger_date
            row.cb3_halted = state.cb3_halted
            row.cb4_halt_until = state.cb4_halt_until
            row.equity_log_json = equity_log_json
            row.updated_at = utcnow()
    except Exception:
        logger.error("save_cb_state failed", exc_info=True)
