"""
Universe state definitions — TickerState enum and FilterCheckResult.

Shared between UniverseManager (state transitions) and SignalEngine
(OBSERVATION mode check before generating trade signals).

Supplements reference: Doc 3, Section 3.1 (state machine)
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class TickerState(str, Enum):
    """The five possible states in the Arconian universe state machine."""

    CANDIDATE    = "CANDIDATE"
    OBSERVATION  = "OBSERVATION"
    ACTIVE       = "ACTIVE"
    SUSPENDED    = "SUSPENDED"
    REMOVED      = "REMOVED"


# States where signal data is still collected (but not necessarily traded)
DATA_COLLECTION_STATES: frozenset[TickerState] = frozenset({
    TickerState.OBSERVATION,
    TickerState.ACTIVE,
    TickerState.SUSPENDED,  # existing positions still monitored
})

# States where new trade signals may be generated
TRADEABLE_STATES: frozenset[TickerState] = frozenset({
    TickerState.ACTIVE,
})


@dataclass
class FilterCheckResult:
    """
    Result of running all 6 universe filter checks on a single ticker.

    Each individual check is True (pass), False (fail), or None (data
    unavailable — treated as inconclusive, not as a failure).

    Supplements reference: Doc 3, Section 3.2 (Daily Universe Check)
    """

    ticker: str
    checked_at: datetime

    # ---- Individual filter results (None = data unavailable) ----
    market_cap_ok: Optional[bool] = None          # $500M–$2B
    daily_dollar_vol_ok: Optional[bool] = None    # 20d avg ≥ $5M
    midday_dollar_vol_ok: Optional[bool] = None   # 20d avg ≥ $1.5M
    spread_ok: Optional[bool] = None              # 20d avg ≤ 40 bps
    options_oi_ok: Optional[bool] = None          # total OI ≥ 1,500
    options_strike_ok: Optional[bool] = None      # ≥ 4 strikes with OI
    history_days_ok: Optional[bool] = None        # ≥ 60 trading days

    # ---- Observed values (for logging / debugging) ----
    market_cap_mm: Optional[float] = None
    daily_dollar_vol_mm: Optional[float] = None
    midday_dollar_vol_mm: Optional[float] = None
    spread_bps: Optional[float] = None
    options_oi: Optional[int] = None
    options_strike_count: Optional[int] = None
    history_days: Optional[int] = None

    @property
    def all_pass(self) -> bool:
        """
        True if every check with available data passed.

        Checks with None (data unavailable) are excluded from consideration —
        they do not count as a failure. This prevents Schwab outages from
        incorrectly suspending otherwise healthy tickers.
        """
        checks = [
            self.market_cap_ok,
            self.daily_dollar_vol_ok,
            self.midday_dollar_vol_ok,
            self.spread_ok,
            self.options_oi_ok,
            self.options_strike_ok,
            self.history_days_ok,
        ]
        return all(c is True or c is None for c in checks)

    @property
    def is_inconclusive(self) -> bool:
        """
        True when NO filter check had data — every result is None.

        A fully-inconclusive result means a total data outage (e.g. both
        yfinance and Schwab unreachable), not a clean pass. ``all_pass``
        cannot tell the two apart: it returns True for an all-None result
        because, vacuously, "every available check passed." Callers driving
        the suspension/reactivation state machine must consult this FIRST and
        make no counter changes on an outage day — otherwise an outage
        silently resets fail streaks and advances reactivation hysteresis,
        which can reactivate a genuinely-suspended ticker (F-14).
        """
        checks = [
            self.market_cap_ok,
            self.daily_dollar_vol_ok,
            self.midday_dollar_vol_ok,
            self.spread_ok,
            self.options_oi_ok,
            self.options_strike_ok,
            self.history_days_ok,
        ]
        return all(c is None for c in checks)

    @property
    def primary_failing_filter(self) -> Optional[str]:
        """
        Return the name of the first filter that explicitly failed (False).

        Used for suspension hysteresis: only suspend if the SAME filter
        fails for N consecutive days.

        Returns None if all available checks passed.
        """
        if self.market_cap_ok is False:
            return "market_cap"
        if self.daily_dollar_vol_ok is False:
            return "daily_dollar_vol"
        if self.midday_dollar_vol_ok is False:
            return "midday_dollar_vol"
        if self.spread_ok is False:
            return "spread"
        if self.options_oi_ok is False:
            return "options_oi"
        if self.options_strike_ok is False:
            return "options_strike"
        if self.history_days_ok is False:
            return "history_days"
        return None

    @property
    def failure_summary(self) -> str:
        """Human-readable string of all failing checks."""
        failures = []
        if self.market_cap_ok is False:
            failures.append(f"market_cap={self.market_cap_mm:.0f}mm" if self.market_cap_mm else "market_cap=unknown")
        if self.daily_dollar_vol_ok is False:
            failures.append(f"daily_vol={self.daily_dollar_vol_mm:.1f}mm" if self.daily_dollar_vol_mm else "daily_vol=unknown")
        if self.midday_dollar_vol_ok is False:
            failures.append(f"midday_vol={self.midday_dollar_vol_mm:.1f}mm" if self.midday_dollar_vol_mm else "midday_vol=unknown")
        if self.spread_ok is False:
            failures.append(f"spread={self.spread_bps:.0f}bps" if self.spread_bps else "spread=unknown")
        if self.options_oi_ok is False:
            failures.append(f"options_oi={self.options_oi}" if self.options_oi else "options_oi=unknown")
        if self.options_strike_ok is False:
            failures.append(f"options_strikes={self.options_strike_count}" if self.options_strike_count else "options_strikes=unknown")
        if self.history_days_ok is False:
            failures.append(f"history={self.history_days}d" if self.history_days else "history=unknown")
        return "; ".join(failures) if failures else "all pass"
