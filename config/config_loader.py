"""
Configuration loader for Arconian.

Loads config/arconian_config.yaml, validates all required keys exist with
correct types, and hashes the config on startup to detect and log changes
to the parameter_history table.

Secrets (API keys, tokens) are NOT stored in the YAML — they are read from
environment variables via .env. This module only handles non-secret params.

Supplements reference: Doc 2 (Configuration Parameter Registry)
"""

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-section dataclasses
# ---------------------------------------------------------------------------

@dataclass
class UniverseConfig:
    market_cap_min_mm: float
    market_cap_max_mm: float
    min_daily_dollar_vol_mm: float
    min_midday_dollar_vol_mm: float
    max_spread_bps: int
    min_options_oi: int
    min_options_strike_count: int
    min_trading_days: int
    observation_mode_threshold_days: int
    delay_score_primary_threshold: float
    refresh_day: str
    suspension_consecutive_fail_days: int


@dataclass
class SignalConfig:
    weight_volume: float
    weight_return: float
    weight_options: float
    weight_sector_rs: float
    weight_delay: float
    weight_floor: float
    retail_penalty_weight: float
    options_enabled: bool
    volume_windows: list[int]
    return_window: int
    sector_rs_window: int
    options_min_daily_volume: int
    options_min_trade_sizes: int
    iv_range_min_pct_points: int
    ic_recalibration_period_days: int
    ic_decay_threshold: float
    ic_consecutive_periods_for_flag: int
    earnings_exclusion_days_before: int
    earnings_exclusion_days_after: int
    corp_action_secondary_exclusion_days: int
    corp_action_reverse_split_exclusion_days: int
    corp_action_merger_exclusion: str
    corp_action_spac_exclusion_days: int
    corp_action_ticker_change_exclusion_days: int


@dataclass
class RiskConfig:
    base_risk_per_trade: float
    phase1_risk_per_trade: float
    atr_stop_multiplier: float
    catalyst_atr_premium: float
    max_position_pct: float
    max_concurrent_positions: int
    max_sector_positions: int
    max_sector_capital_pct: float
    max_correlated_cluster: int
    correlation_threshold: float
    correlation_lookback_days: int
    max_overnight_exposure_pct: float
    vix_elevated_threshold: float
    vix_crisis_threshold: float
    iwm_elevated_10d_return: float
    iwm_crisis_20d_return: float
    consecutive_loss_trigger: int
    drawdown_7pct_position_limit: int
    drawdown_7pct_recovery_days: int
    drawdown_12pct_halt: bool
    single_day_loss_halt_pct: float
    single_day_loss_halt_days: int
    # Full risk-engine rollout (Tier-3 #18): 'off' keeps the minimal dedup+cap
    # auto-trade path; 'shadow' also runs RiskEngine.approve_new_position and
    # LOGS its accept/reject decisions without enforcing them; 'enforce' lets the
    # risk engine actually gate which paper trades open. Default 'off'.
    full_engine_mode: str = "off"


@dataclass
class ExecutionConfig:
    paper_trading: bool
    limit_buffer_bps: int
    fill_window_seconds: int
    partial_fill_min_pct: float
    partial_fill_accept_pct: float
    tp1_exit_pct: float
    tp1_r_multiple: float
    trailing_stop_atr_multiple: float
    time_stop_days: int
    scan_times_et: list[str]
    pass1_top_n: int
    auto_paper_trade: bool = False
    auto_paper_trade_min_score: float = 0.70
    account_equity: float = 10_000.0


@dataclass
class CostConfig:
    half_spread_bps: int
    slippage_bps: int
    adverse_selection_bps: int
    borrow_cost_bps_default: int
    working_roundtrip_long_bps: int
    working_roundtrip_short_bps: int
    exceedance_threshold_pct: float
    exceedance_rolling_window: int


@dataclass
class TaxConfig:
    account_type: str
    effective_rate_taxable: float
    effective_rate_roth: float


@dataclass
class DeadmanConfig:
    scan_timeout_minutes: int
    widen_stops_minutes: int
    force_close_minutes: int
    action_mode: str = "alert_only"   # "alert_only" | "full"


@dataclass
class MLConfig:
    min_events_logistic: int
    min_events_lightgbm: int
    sharpe_improvement_threshold: float
    purge_days: int
    embargo_days: int
    walk_forward_window_days: int
    calibration_slope_tolerance: float
    lgbm_max_depth: int
    lgbm_num_leaves: int
    lgbm_min_data_in_leaf: int
    lgbm_learning_rate: float


# ---------------------------------------------------------------------------
# Validation spec
# Maps dotted key paths to (expected_type, optional_validator_fn)
# ---------------------------------------------------------------------------

_REQUIRED_KEYS: dict[str, type | tuple[type, ...]] = {
    # universe
    "universe.market_cap_min_mm": (int, float),
    "universe.market_cap_max_mm": (int, float),
    "universe.min_daily_dollar_vol_mm": (int, float),
    "universe.min_midday_dollar_vol_mm": (int, float),
    "universe.max_spread_bps": int,
    "universe.min_options_oi": int,
    "universe.min_options_strike_count": int,
    "universe.min_trading_days": int,
    "universe.observation_mode_threshold_days": int,
    "universe.delay_score_primary_threshold": (int, float),
    "universe.refresh_day": str,
    "universe.suspension_consecutive_fail_days": int,
    # signal
    "signal.weight_volume": (int, float),
    "signal.weight_return": (int, float),
    "signal.weight_options": (int, float),
    "signal.weight_sector_rs": (int, float),
    "signal.weight_delay": (int, float),
    "signal.weight_floor": (int, float),
    "signal.retail_penalty_weight": (int, float),
    "signal.options_enabled": bool,
    "signal.volume_windows": list,
    "signal.return_window": int,
    "signal.sector_rs_window": int,
    "signal.options_min_daily_volume": int,
    "signal.options_min_trade_sizes": int,
    "signal.iv_range_min_pct_points": int,
    "signal.ic_recalibration_period_days": int,
    "signal.ic_decay_threshold": (int, float),
    "signal.ic_consecutive_periods_for_flag": int,
    "signal.earnings_exclusion_days_before": int,
    "signal.earnings_exclusion_days_after": int,
    "signal.corp_action_secondary_exclusion_days": int,
    "signal.corp_action_reverse_split_exclusion_days": int,
    "signal.corp_action_merger_exclusion": str,
    "signal.corp_action_spac_exclusion_days": int,
    "signal.corp_action_ticker_change_exclusion_days": int,
    # risk
    "risk.base_risk_per_trade": (int, float),
    "risk.phase1_risk_per_trade": (int, float),
    "risk.atr_stop_multiplier": (int, float),
    "risk.catalyst_atr_premium": (int, float),
    "risk.max_position_pct": (int, float),
    "risk.max_concurrent_positions": int,
    "risk.max_sector_positions": int,
    "risk.max_sector_capital_pct": (int, float),
    "risk.max_correlated_cluster": int,
    "risk.correlation_threshold": (int, float),
    "risk.correlation_lookback_days": int,
    "risk.max_overnight_exposure_pct": (int, float),
    "risk.vix_elevated_threshold": (int, float),
    "risk.vix_crisis_threshold": (int, float),
    "risk.iwm_elevated_10d_return": (int, float),
    "risk.iwm_crisis_20d_return": (int, float),
    "risk.full_engine_mode": str,
    "risk.consecutive_loss_trigger": int,
    "risk.drawdown_7pct_position_limit": int,
    "risk.drawdown_7pct_recovery_days": int,
    "risk.drawdown_12pct_halt": bool,
    "risk.single_day_loss_halt_pct": (int, float),
    "risk.single_day_loss_halt_days": int,
    # execution
    "execution.paper_trading": bool,
    "execution.limit_buffer_bps": int,
    "execution.fill_window_seconds": int,
    "execution.partial_fill_min_pct": (int, float),
    "execution.partial_fill_accept_pct": (int, float),
    "execution.tp1_exit_pct": (int, float),
    "execution.tp1_r_multiple": (int, float),
    "execution.trailing_stop_atr_multiple": (int, float),
    "execution.time_stop_days": int,
    "execution.scan_times_et": list,
    "execution.pass1_top_n": int,
    # These three directly control live auto-trading; validate them explicitly
    # so a deletion can't silently fall back to dataclass defaults (e.g.
    # account_equity 10_000 vs the configured 26_000 → 2.6x sizing change).
    "execution.auto_paper_trade": bool,
    "execution.auto_paper_trade_min_score": (int, float),
    "execution.account_equity": (int, float),
    # cost
    "cost.half_spread_bps": int,
    "cost.slippage_bps": int,
    "cost.adverse_selection_bps": int,
    "cost.borrow_cost_bps_default": int,
    "cost.working_roundtrip_long_bps": int,
    "cost.working_roundtrip_short_bps": int,
    "cost.exceedance_threshold_pct": (int, float),
    "cost.exceedance_rolling_window": int,
    # tax
    "tax.account_type": str,
    "tax.effective_rate_taxable": (int, float),
    "tax.effective_rate_roth": (int, float),
    # deadman
    "deadman.scan_timeout_minutes": int,
    "deadman.widen_stops_minutes": int,
    "deadman.force_close_minutes": int,
    "deadman.action_mode": str,
    # ml
    "ml.min_events_logistic": int,
    "ml.min_events_lightgbm": int,
    "ml.sharpe_improvement_threshold": (int, float),
    "ml.purge_days": int,
    "ml.embargo_days": int,
    "ml.walk_forward_window_days": int,
    "ml.calibration_slope_tolerance": (int, float),
    "ml.lgbm_max_depth": int,
    "ml.lgbm_num_leaves": int,
    "ml.lgbm_min_data_in_leaf": int,
    "ml.lgbm_learning_rate": (int, float),
}


# ---------------------------------------------------------------------------
# ConfigLoader
# ---------------------------------------------------------------------------

class ConfigLoader:
    """
    Loads and validates arconian_config.yaml. Provides typed attribute access
    via nested dataclass sections.

    Usage:
        config = ConfigLoader("config/arconian_config.yaml")
        max_pos = config.risk.max_concurrent_positions   # int
        weights = config.signal.weight_volume            # float

    The raw dict is available via config.raw for dynamic access.
    """

    def __init__(self, config_path: str | Path = "config/arconian_config.yaml") -> None:
        self._path = Path(config_path)
        self.raw: dict[str, Any] = self._load()
        self._validate(self.raw)
        self._hash: str = self._compute_hash()

        u = self.raw["universe"]
        s = self.raw["signal"]
        r = self.raw["risk"]
        e = self.raw["execution"]
        c = self.raw["cost"]
        t = self.raw["tax"]
        d = self.raw["deadman"]
        m = self.raw["ml"]

        self.universe = UniverseConfig(**u)
        self.signal = SignalConfig(**s)
        self.risk = RiskConfig(**r)
        self.execution = ExecutionConfig(**e)
        self.cost = CostConfig(**c)
        self.tax = TaxConfig(**t)
        self.deadman = DeadmanConfig(**d)
        self.ml = MLConfig(**m)

        logger.info("Config loaded from %s (sha256: %s)", self._path, self._hash[:12])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def config_hash(self) -> str:
        """SHA-256 of the raw YAML file content."""
        return self._hash

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """
        Dynamic access using dot notation.

        Args:
            dotted_key: e.g. 'signal.weight_volume'
            default: returned if key is absent

        Returns:
            The config value, or default.
        """
        parts = dotted_key.split(".")
        node: Any = self.raw
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def diff(self, other_raw: dict[str, Any]) -> list[tuple[str, Any, Any]]:
        """
        Return list of (dotted_key, old_value, new_value) for changed params.

        Used by parameter_tracker.py to detect config changes on startup.

        Args:
            other_raw: Previously-saved raw config dict to compare against.

        Returns:
            List of (key, old, new) tuples for changed values.
        """
        changes: list[tuple[str, Any, Any]] = []
        self._diff_recursive(other_raw, self.raw, "", changes)
        return changes

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            raise FileNotFoundError(
                f"Config file not found: {self._path}. "
                "Copy config/arconian_config.yaml to get started."
            )
        with self._path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            raise ValueError(f"Config file {self._path} did not parse to a mapping.")
        return data

    def _validate(self, data: dict[str, Any]) -> None:
        """Validate all required keys exist with correct types. Raises on error."""
        errors: list[str] = []

        for dotted_key, expected_type in _REQUIRED_KEYS.items():
            value = self.get.__func__(  # type: ignore[attr-defined]
                self, dotted_key  # call get before self is fully built
            ) if False else self._get_raw(data, dotted_key)

            if value is None:
                # Distinguish truly missing from explicitly-set null
                if self._get_raw(data, dotted_key, _SENTINEL) is _SENTINEL:
                    errors.append(f"Missing required key: {dotted_key}")
                    continue

            if not isinstance(value, expected_type):  # type: ignore[arg-type]
                errors.append(
                    f"Type error for {dotted_key}: "
                    f"expected {expected_type}, got {type(value).__name__} ({value!r})"
                )

        # Signal weights: warn if unusually low sum, but don't error.
        # The signal engine normalises weights at runtime (redistributes weight
        # from NULL signals). The whitepaper's published defaults sum to 0.90;
        # treating that as an error would be wrong.
        if "signal" in data:
            s = data["signal"]
            weight_keys = ["weight_volume", "weight_return", "weight_options",
                           "weight_sector_rs", "weight_delay"]
            if all(k in s for k in weight_keys):
                total = sum(float(s[k]) for k in weight_keys)
                if total > 1.01:
                    errors.append(
                        f"Signal weights sum to {total:.4f} which exceeds 1.0. "
                        "Reduce one or more weights."
                    )
                elif total < 0.50:
                    errors.append(
                        f"Signal weights sum to only {total:.4f}. "
                        "Check for a misconfiguration."
                    )

        if errors:
            raise ValueError(
                "Config validation failed with {} error(s):\n  {}".format(
                    len(errors), "\n  ".join(errors)
                )
            )

    def _compute_hash(self) -> str:
        """SHA-256 of the raw YAML file bytes."""
        return hashlib.sha256(self._path.read_bytes()).hexdigest()

    @staticmethod
    def _get_raw(data: dict[str, Any], dotted_key: str, default: Any = None) -> Any:
        parts = dotted_key.split(".")
        node: Any = data
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @staticmethod
    def _diff_recursive(
        old: Any,
        new: Any,
        prefix: str,
        changes: list[tuple[str, Any, Any]],
    ) -> None:
        if isinstance(old, dict) and isinstance(new, dict):
            all_keys = set(old) | set(new)
            for k in sorted(all_keys):
                full_key = f"{prefix}.{k}" if prefix else k
                ConfigLoader._diff_recursive(
                    old.get(k), new.get(k), full_key, changes
                )
        else:
            if old != new:
                changes.append((prefix, old, new))


_SENTINEL = object()
