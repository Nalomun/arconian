"""Initial schema — all Arconian tables.

Revision ID: 0001
Revises:
Create Date: 2026-04-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ----------------------------------------------------------------
    # parameter_history — no foreign keys
    # ----------------------------------------------------------------
    op.create_table(
        "parameter_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("parameter_path", sa.String(100), nullable=False),
        sa.Column("old_value", sa.Text, nullable=False),
        sa.Column("new_value", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("triggered_by", sa.String(50), nullable=False),
    )

    # ----------------------------------------------------------------
    # universe_state — no foreign keys
    # ----------------------------------------------------------------
    op.create_table(
        "universe_state",
        sa.Column("ticker", sa.String(10), primary_key=True),
        sa.Column("state", sa.String(15), nullable=False),
        sa.Column("state_since", sa.DateTime, nullable=False),
        sa.Column("history_days", sa.Integer, nullable=True),
        sa.Column("suspension_reason", sa.Text, nullable=True),
        sa.Column("suspension_start", sa.Date, nullable=True),
        sa.Column("consecutive_fail_days", sa.Integer, nullable=False, server_default="0"),
        sa.Column("consecutive_pass_days", sa.Integer, nullable=False, server_default="0"),
        sa.Column("delay_score", sa.Float, nullable=True),
        sa.Column("delay_score_as_of", sa.Date, nullable=True),
        sa.Column("last_checked", sa.DateTime, nullable=True),
        sa.Column("manual_block", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("manual_block_reason", sa.Text, nullable=True),
        sa.CheckConstraint(
            "state IN ('CANDIDATE','OBSERVATION','ACTIVE','SUSPENDED','REMOVED')",
            name="ck_universe_state_valid",
        ),
    )

    # ----------------------------------------------------------------
    # overrides — no foreign keys
    # ----------------------------------------------------------------
    op.create_table(
        "overrides",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(10), nullable=False, index=True),
        sa.Column("override_type", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("created_by", sa.String(50), nullable=False, server_default="'manual'"),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default="1"),
        sa.CheckConstraint(
            "override_type IN ('block','force_review','corp_action_manual')",
            name="ck_overrides_type_valid",
        ),
    )
    op.create_index("ix_overrides_ticker", "overrides", ["ticker"])

    # ----------------------------------------------------------------
    # ic_history — no foreign keys
    # ----------------------------------------------------------------
    op.create_table(
        "ic_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("computed_at", sa.DateTime, nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("signal_component", sa.String(20), nullable=False),
        sa.Column("segment", sa.String(30), nullable=False, server_default="'all'"),
        sa.Column("ic_value", sa.Float, nullable=True),
        sa.Column("ic_count", sa.Integer, nullable=True),
        sa.Column("decay_flag", sa.Boolean, nullable=False, server_default="0"),
        sa.CheckConstraint(
            "signal_component IN ('volume','return','options','sector_rs','delay','composite')",
            name="ck_ic_history_component_valid",
        ),
        sa.UniqueConstraint(
            "computed_at", "signal_component", "segment",
            name="uq_ic_history_period_component_segment",
        ),
    )

    # ----------------------------------------------------------------
    # trades — no foreign keys; referenced by signal_log
    # ----------------------------------------------------------------
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ticker", sa.String(10), nullable=False),
        sa.Column("direction", sa.String(5), nullable=False),
        sa.Column("status", sa.String(15), nullable=False, server_default="'open'"),
        sa.Column("entry_price", sa.Float, nullable=True),
        sa.Column("entry_time", sa.DateTime, nullable=True),
        sa.Column("shares", sa.Float, nullable=True),
        sa.Column("stop_price", sa.Float, nullable=True),
        sa.Column("target_price", sa.Float, nullable=True),
        sa.Column("atr_at_entry", sa.Float, nullable=True),
        sa.Column("exit_price", sa.Float, nullable=True),
        sa.Column("exit_time", sa.DateTime, nullable=True),
        sa.Column("exit_reason", sa.String(20), nullable=True),
        sa.Column("realized_pnl", sa.Float, nullable=True),
        sa.Column("realized_r_multiple", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.CheckConstraint("direction IN ('long','short')", name="ck_trades_direction_valid"),
        sa.CheckConstraint(
            "status IN ('open','closed','cancelled')", name="ck_trades_status_valid"
        ),
        sa.CheckConstraint(
            "exit_reason IN ('stop','target','time_stop','manual','force_close') OR exit_reason IS NULL",
            name="ck_trades_exit_reason_valid",
        ),
    )
    op.create_index("ix_trades_ticker", "trades", ["ticker"])

    # ----------------------------------------------------------------
    # signal_log — references trades.id
    # ----------------------------------------------------------------
    op.create_table(
        "signal_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        # Scan metadata
        sa.Column("scan_timestamp", sa.DateTime, nullable=False),
        sa.Column("scan_type", sa.String(10), nullable=False),
        sa.Column("ticker", sa.String(10), nullable=False),
        # Feature vector
        sa.Column("volume_pctile_20d", sa.Float, nullable=True),
        sa.Column("volume_pctile_60d", sa.Float, nullable=True),
        sa.Column("volume_pctile_120d", sa.Float, nullable=True),
        sa.Column("return_pctile_60d", sa.Float, nullable=True),
        sa.Column("options_composite", sa.Float, nullable=True),
        sa.Column("sector_rs_percentile", sa.Float, nullable=True),
        sa.Column("delay_score", sa.Float, nullable=True),
        sa.Column("delay_score_as_of_signal", sa.Float, nullable=True),
        sa.Column("retail_attention_score", sa.Float, nullable=True),
        sa.Column("composite_score_raw", sa.Float, nullable=True),
        sa.Column("composite_score", sa.Float, nullable=True),
        # Weights at signal time
        sa.Column("weight_volume", sa.Float, nullable=True),
        sa.Column("weight_return", sa.Float, nullable=True),
        sa.Column("weight_options", sa.Float, nullable=True),
        sa.Column("weight_sector_rs", sa.Float, nullable=True),
        sa.Column("weight_delay", sa.Float, nullable=True),
        # Market context
        sa.Column("spy_return_1d", sa.Float, nullable=True),
        sa.Column("vix_level", sa.Float, nullable=True),
        sa.Column("iwm_return_10d", sa.Float, nullable=True),
        sa.Column("iwm_return_20d", sa.Float, nullable=True),
        sa.Column("sector_etf", sa.String(10), nullable=True),
        sa.Column("market_cap_mm", sa.Float, nullable=True),
        sa.Column("avg_daily_dollar_vol", sa.Float, nullable=True),
        sa.Column("midday_dollar_vol", sa.Float, nullable=True),
        sa.Column("bid_ask_spread_bps", sa.Float, nullable=True),
        sa.Column("atr_20", sa.Float, nullable=True),
        # Event context
        sa.Column("days_to_next_earnings", sa.Integer, nullable=True),
        sa.Column("days_since_last_earnings", sa.Integer, nullable=True),
        sa.Column("earnings_proximity_tag", sa.String(25), nullable=True),
        sa.Column("corporate_action_flag", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("corporate_action_type", sa.String(30), nullable=True),
        sa.Column("catalyst_flag", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("history_status", sa.String(10), nullable=False, server_default="'full'"),
        # Directional confirmation
        sa.Column("direction_signal", sa.String(10), nullable=True),
        sa.Column("prior_day_vwap", sa.Float, nullable=True),
        sa.Column("borrow_available", sa.Boolean, nullable=True),
        # Outcomes (filled asynchronously)
        sa.Column("return_1d", sa.Float, nullable=True),
        sa.Column("return_3d", sa.Float, nullable=True),
        sa.Column("return_from_signal_time_3d", sa.Float, nullable=True),
        sa.Column("max_adverse_excursion", sa.Float, nullable=True),
        sa.Column("max_favorable_excursion", sa.Float, nullable=True),
        sa.Column("outcome_label", sa.Integer, nullable=True),
        # Trade linkage
        sa.Column("was_traded", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("trade_id", sa.Integer, sa.ForeignKey("trades.id"), nullable=True),
        sa.Column("entry_price", sa.Float, nullable=True),
        sa.Column("exit_price", sa.Float, nullable=True),
        sa.Column("realized_pnl", sa.Float, nullable=True),
        sa.Column("realized_pnl_after_tax", sa.Float, nullable=True),
        sa.Column("realized_r_multiple", sa.Float, nullable=True),
        # Execution quality
        sa.Column("slippage_bps", sa.Float, nullable=True),
        sa.Column("cost_spread_bps", sa.Float, nullable=True),
        sa.Column("cost_impact_bps", sa.Float, nullable=True),
        sa.Column("cost_adverse_selection_bps", sa.Float, nullable=True),
        sa.Column("cost_borrow_bps", sa.Float, nullable=True),
        sa.Column("partial_fill_flag", sa.String(25), nullable=True),
        sa.Column("fill_pct", sa.Float, nullable=True),
        # Constraints
        sa.CheckConstraint(
            "scan_type IN ('open','midday','preclose')",
            name="ck_signal_log_scan_type_valid",
        ),
        sa.CheckConstraint(
            "earnings_proximity_tag IN ('excluded','post_earnings_drift','normal') OR earnings_proximity_tag IS NULL",
            name="ck_signal_log_earnings_tag_valid",
        ),
        sa.CheckConstraint(
            "direction_signal IN ('bullish','bearish','ambiguous') OR direction_signal IS NULL",
            name="ck_signal_log_direction_valid",
        ),
        sa.CheckConstraint(
            "history_status IN ('full','short')",
            name="ck_signal_log_history_status_valid",
        ),
        sa.CheckConstraint(
            "partial_fill_flag IN ('full','partial_accepted','partial_abandoned') OR partial_fill_flag IS NULL",
            name="ck_signal_log_fill_flag_valid",
        ),
    )
    op.create_index("ix_signal_log_scan_timestamp", "signal_log", ["scan_timestamp"])
    op.create_index("ix_signal_log_ticker", "signal_log", ["ticker"])

    # ----------------------------------------------------------------
    # outcome_prices — references signal_log.id
    # ----------------------------------------------------------------
    op.create_table(
        "outcome_prices",
        sa.Column(
            "signal_id",
            sa.Integer,
            sa.ForeignKey("signal_log.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("day_offset", sa.Integer, primary_key=True),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("high", sa.Float, nullable=False),
        sa.Column("low", sa.Float, nullable=False),
        sa.Column("close", sa.Float, nullable=False),
        sa.CheckConstraint(
            "day_offset BETWEEN 0 AND 3",
            name="ck_outcome_prices_day_offset_range",
        ),
        sa.UniqueConstraint(
            "signal_id", "day_offset",
            name="uq_outcome_prices_signal_day",
        ),
    )


def downgrade() -> None:
    op.drop_table("outcome_prices")
    op.drop_table("signal_log")
    op.drop_table("trades")
    op.drop_table("ic_history")
    op.drop_table("overrides")
    op.drop_table("universe_state")
    op.drop_table("parameter_history")
