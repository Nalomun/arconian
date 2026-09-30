"""
SQLAlchemy ORM models for all Arconian database tables.

Tables (creation order respects FK dependencies):
  1. parameter_history  — config change audit log (supplements Doc 2)
  2. universe_state     — per-ticker state machine (supplements Doc 3 §3.1)
  3. overrides          — manual blocklist / force-review flags (supplements Doc 3 §3.6)
  4. ic_history         — per-signal IC time series
  5. trades             — executed trades (Phase 2+; referenced by signal_log)
  6. signal_log         — every candidate event scored by the signal engine (whitepaper §7.1)
  7. outcome_prices     — daily OHLC outcomes for each signal event (whitepaper §7.2)

Whitepaper reference: Section 7
Supplements reference: Doc 2 (parameter_history), Doc 3 §3.1, §3.6
"""

from datetime import date, datetime
from timeutils import utcnow
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# parameter_history
# No foreign keys — safe to create first.
# ---------------------------------------------------------------------------
class ParameterHistory(Base):
    """
    Audit log of every configuration parameter change.

    Written by config/config_loader.py on startup when a change is detected,
    and by the IC recalibration task when signal weights are updated.

    Supplements reference: Doc 2 (Configuration Parameter Registry)
    """

    __tablename__ = "parameter_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    parameter_path: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="e.g. 'signal.weight_volume'"
    )
    old_value: Mapped[str] = mapped_column(Text, nullable=False)
    new_value: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(
        Text, nullable=False, comment="e.g. 'IC recalibration period 3'"
    )
    triggered_by: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="'ic_recalibration', 'manual', 'circuit_breaker', 'startup'",
    )


# ---------------------------------------------------------------------------
# circuit_breaker_state
# Singleton row (id=1) — survives restarts so streaks/drawdown/halts persist.
# No foreign keys.
# ---------------------------------------------------------------------------


class CircuitBreakerStateRow(Base):
    """
    Persisted snapshot of the four circuit-breaker counters (Tier-3 #18 / F-17).

    CircuitBreakerManager kept state in memory only, so every daemon restart
    zeroed consecutive losses, rolling drawdown, and any active CB3/CB4 halt —
    a restart could silently resume trading mid-halt. One singleton row (id=1,
    one account in Phase 1) is loaded at startup and rewritten after each
    record_trade/record_day.
    """

    __tablename__ = "circuit_breaker_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    consecutive_losses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cb1_trades_remaining: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cb2_trigger_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    cb3_halted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cb4_halt_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # JSON list of [iso_date, equity] for the rolling drawdown window.
    equity_log_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )


# ---------------------------------------------------------------------------
# universe_state
# No foreign keys.
# ---------------------------------------------------------------------------
class UniverseState(Base):
    """
    Per-ticker state machine state and filter history.

    States: CANDIDATE → OBSERVATION → ACTIVE → SUSPENDED → REMOVED
    Hysteresis: 5 consecutive filter failures → SUSPENDED;
                3 consecutive passes → re-ACTIVE.

    Supplements reference: Doc 3, Section 3.1
    """

    __tablename__ = "universe_state"
    __table_args__ = (
        CheckConstraint(
            "state IN ('CANDIDATE','OBSERVATION','ACTIVE','SUSPENDED','REMOVED')",
            name="ck_universe_state_valid",
        ),
    )

    ticker: Mapped[str] = mapped_column(String(10), primary_key=True)
    state: Mapped[str] = mapped_column(String(15), nullable=False)
    state_since: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # History / eligibility tracking
    history_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Suspension tracking
    suspension_reason: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Which filter failed, e.g. 'midday_vol < 1.5M'",
    )
    suspension_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    consecutive_fail_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_pass_days: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
        comment="For re-entry hysteresis",
    )

    # Price delay score (versioned — computed monthly)
    delay_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    delay_score_as_of: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    # Metadata
    last_checked: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    manual_block: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    manual_block_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# overrides
# No foreign keys.
# ---------------------------------------------------------------------------
class Override(Base):
    """
    Manual blocklist and force-review flags.

    Always checked before scoring any ticker. This is the safety valve
    that lets operators exclude tickers (e.g., pending litigation,
    known data quality issues) without touching config.

    Supplements reference: Doc 3, Section 3.6
    """

    __tablename__ = "overrides"
    __table_args__ = (
        CheckConstraint(
            "override_type IN ('block','force_review','corp_action_manual')",
            name="ck_overrides_type_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    override_type: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    created_by: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="manual",
        comment="'manual' or 'auto'",
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="NULL = permanent until explicitly removed",
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# ---------------------------------------------------------------------------
# ic_history
# No foreign keys.
# ---------------------------------------------------------------------------
class ICHistory(Base):
    """
    Information Coefficient time series per signal component and segment.

    Written by journal/ic_tracker.py every ic_recalibration_period_days.
    Used to detect signal decay and trigger weight recalibration.

    The segment column allows sliced IC analysis (e.g., 'open_scan' vs
    'preclose_scan', 'high_retail' vs 'low_retail').
    """

    __tablename__ = "ic_history"
    __table_args__ = (
        UniqueConstraint(
            "computed_at", "signal_component", "segment",
            name="uq_ic_history_period_component_segment",
        ),
        CheckConstraint(
            "signal_component IN ('volume','return','options','sector_rs','delay','composite')",
            name="ck_ic_history_component_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment="When this IC computation was run",
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    signal_component: Mapped[str] = mapped_column(String(20), nullable=False)
    segment: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="all",
        comment="'all','open_scan','midday_scan','preclose_scan','pre_earnings','post_earnings','high_retail','low_retail'",
    )

    ic_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="Spearman rank correlation vs 3d forward return"
    )
    ic_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="Number of observations used in computation"
    )
    decay_flag: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="True if IC below ic_decay_threshold for ic_consecutive_periods_for_flag periods",
    )


# ---------------------------------------------------------------------------
# trades
# No foreign keys. Referenced by signal_log.trade_id.
# Phase 2+ will flesh this out further.
# ---------------------------------------------------------------------------
class Trade(Base):
    """
    Executed trades. Phase 0/1: table exists but is empty.

    Phase 2+ will populate this via the order manager. The signal_log
    references this table so outcomes can be linked back to the signal
    that generated them.
    """

    __tablename__ = "trades"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('long','short')",
            name="ck_trades_direction_valid",
        ),
        CheckConstraint(
            "status IN ('open','closed','cancelled')",
            name="ck_trades_status_valid",
        ),
        CheckConstraint(
            "exit_reason IN ('stop','target','time_stop','manual','force_close','trailing_stop') OR exit_reason IS NULL",
            name="ck_trades_exit_reason_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(5), nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="open")

    # Entry
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    shares: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="Fractional shares supported"
    )

    # Stops / targets
    stop_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    atr_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # TP1 partial exit (50% at target, trail remainder — whitepaper §4.6)
    tp1_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    shares_remaining: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="Shares held after TP1 partial exit"
    )
    trailing_stop_price: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="Active trailing stop on remainder after TP1"
    )
    tp1_realized_pnl: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="P&L from the TP1 partial exit leg"
    )

    # Exit
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # P&L (total across both legs for TP1 trades)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_r_multiple: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )

    # Relationships
    signal_events: Mapped[list["SignalLog"]] = relationship(
        "SignalLog", back_populates="trade"
    )


# ---------------------------------------------------------------------------
# signal_log
# References: trades.id
# ---------------------------------------------------------------------------
class SignalLog(Base):
    """
    Every candidate event scored by the signal engine, whether traded or not.

    Logging ALL candidates (not just traded ones) prevents survivorship bias
    when this data is used for ML training in Phase 3.

    Feature values are versioned at signal time (e.g. delay_score_as_of_signal)
    to prevent temporal leakage in ML pipelines.

    Whitepaper reference: Section 7.1
    """

    __tablename__ = "signal_log"
    __table_args__ = (
        CheckConstraint(
            "scan_type IN ('open','midday','preclose')",
            name="ck_signal_log_scan_type_valid",
        ),
        CheckConstraint(
            "earnings_proximity_tag IN ('excluded','post_earnings_drift','normal') OR earnings_proximity_tag IS NULL",
            name="ck_signal_log_earnings_tag_valid",
        ),
        CheckConstraint(
            "direction_signal IN ('bullish','bearish','ambiguous') OR direction_signal IS NULL",
            name="ck_signal_log_direction_valid",
        ),
        CheckConstraint(
            "history_status IN ('full','short')",
            name="ck_signal_log_history_status_valid",
        ),
        CheckConstraint(
            "partial_fill_flag IN ('full','partial_accepted','partial_abandoned') OR partial_fill_flag IS NULL",
            name="ck_signal_log_fill_flag_valid",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ---- Scan metadata ----
    scan_timestamp: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        index=True,
        comment="Exact scan time in UTC (convert to US/Eastern for display)",
    )
    scan_type: Mapped[str] = mapped_column(
        String(10), nullable=False, comment="'open','midday','preclose'"
    )
    ticker: Mapped[str] = mapped_column(String(10), nullable=False, index=True)

    # ---- Feature vector ----
    # All continuous; all percentile ranks [0,1] unless noted.
    volume_pctile_20d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume_pctile_60d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume_pctile_120d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_pctile_60d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    options_composite: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="NULL if options safeguards not met"
    )
    sector_rs_percentile: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    delay_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    delay_score_as_of_signal: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        comment="Versioned: Hou-Moskowitz value active at signal time (prevents ML leakage)",
    )
    retail_attention_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="Input to retail attention penalty"
    )
    composite_score_raw: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="Weighted composite before retail penalty"
    )
    composite_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="Final score after retail penalty"
    )

    # ---- Signal weights at scan time ----
    # Stored so ML training can account for weight drift over time.
    weight_volume: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    weight_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    weight_options: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    weight_sector_rs: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    weight_delay: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ---- Market context ----
    spy_return_1d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vix_level: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    iwm_return_10d: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="Small-cap regime context"
    )
    iwm_return_20d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sector_etf: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True, comment="e.g. 'XLK', 'XLV'"
    )
    market_cap_mm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_daily_dollar_vol: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    midday_dollar_vol: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bid_ask_spread_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    atr_20: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ---- Event context ----
    days_to_next_earnings: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    days_since_last_earnings: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    earnings_proximity_tag: Mapped[Optional[str]] = mapped_column(
        String(25),
        nullable=True,
        comment="'excluded','post_earnings_drift','normal'",
    )
    corporate_action_flag: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    corporate_action_type: Mapped[Optional[str]] = mapped_column(
        String(30),
        nullable=True,
        comment="'secondary','reverse_split','merger','spac','ticker_change'",
    )
    catalyst_flag: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    history_status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="full",
        comment="'full' = ACTIVE; 'short' = OBSERVATION mode",
    )

    # ---- Directional confirmation ----
    direction_signal: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True, comment="'bullish','bearish','ambiguous'"
    )
    prior_day_vwap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    borrow_available: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True, comment="For bearish signals"
    )

    # ---- Outcomes (filled asynchronously by outcome_collector.py) ----
    return_1d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_3d: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_from_signal_time_3d: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        comment="Signal-time to signal-time return (not open-to-open)",
    )
    max_adverse_excursion: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_favorable_excursion: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    outcome_label: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        comment="+1 upper barrier hit, -1 lower barrier hit, 0 time barrier",
    )

    # ---- Trade linkage (NULL if not traded) ----
    was_traded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trade_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("trades.id"),
        nullable=True,
    )
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_pnl_after_tax: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_r_multiple: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ---- Execution quality (filled by cost_validator.py) ----
    slippage_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cost_spread_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cost_impact_bps: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cost_adverse_selection_bps: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="Residual after spread and impact"
    )
    cost_borrow_bps: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="Short borrow cost for this trade"
    )
    partial_fill_flag: Mapped[Optional[str]] = mapped_column(
        String(25), nullable=True, comment="'full','partial_accepted','partial_abandoned'"
    )
    fill_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="Fraction of intended size actually filled"
    )

    # ---- Relationships ----
    trade: Mapped[Optional["Trade"]] = relationship("Trade", back_populates="signal_events")
    outcome_prices: Mapped[list["OutcomePrice"]] = relationship(
        "OutcomePrice", back_populates="signal_event", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# outcome_prices
# References: signal_log.id
# ---------------------------------------------------------------------------
class OutcomePrice(Base):
    """
    Daily OHLC price observations for each signal event over its hold window.

    Written daily by journal/outcome_collector.py for days 0–3 after the
    signal. Used to compute triple-barrier labels and MAE/MFE statistics.

    Whitepaper reference: Section 7.2
    """

    __tablename__ = "outcome_prices"
    __table_args__ = (
        UniqueConstraint(
            "signal_id", "day_offset",
            name="uq_outcome_prices_signal_day",
        ),
        CheckConstraint(
            "day_offset BETWEEN 0 AND 3",
            name="ck_outcome_prices_day_offset_range",
        ),
    )

    signal_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("signal_log.id", ondelete="CASCADE"),
        primary_key=True,
    )
    day_offset: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        comment="0 = signal day, 1 = day+1, ..., 3 = day+3",
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)

    # Relationship
    signal_event: Mapped["SignalLog"] = relationship(
        "SignalLog", back_populates="outcome_prices"
    )
