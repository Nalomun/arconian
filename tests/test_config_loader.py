"""Tests for config/config_loader.py."""

import copy
import pytest
import yaml
from pathlib import Path

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.config_loader import ConfigLoader


CONFIG_PATH = Path(__file__).parent.parent / "config" / "arconian_config.yaml"


@pytest.fixture
def config():
    return ConfigLoader(CONFIG_PATH)


@pytest.fixture
def raw_config():
    with CONFIG_PATH.open() as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def test_loads_without_error(config):
    """ConfigLoader must load the default config without raising."""
    assert config is not None


def test_config_hash_is_hex_string(config):
    """config_hash must be a non-empty hex string."""
    h = config.config_hash
    assert isinstance(h, str)
    assert len(h) == 64  # SHA-256 = 64 hex chars
    int(h, 16)           # must be valid hex


# ---------------------------------------------------------------------------
# Typed section access
# ---------------------------------------------------------------------------

def test_universe_section_types(config):
    u = config.universe
    assert isinstance(u.market_cap_min_mm, (int, float))
    assert isinstance(u.market_cap_max_mm, (int, float))
    assert isinstance(u.min_trading_days, int)
    assert isinstance(u.observation_mode_threshold_days, int)
    assert isinstance(u.refresh_day, str)


def test_signal_section_types(config):
    s = config.signal
    assert isinstance(s.weight_volume, (int, float))
    assert isinstance(s.volume_windows, list)
    assert all(isinstance(w, int) for w in s.volume_windows)
    assert isinstance(s.corp_action_merger_exclusion, str)


def test_risk_section_types(config):
    r = config.risk
    assert isinstance(r.drawdown_12pct_halt, bool)
    assert isinstance(r.max_concurrent_positions, int)
    assert isinstance(r.phase1_risk_per_trade, float)


def test_execution_section_types(config):
    e = config.execution
    assert isinstance(e.scan_times_et, list)
    assert all(isinstance(t, str) for t in e.scan_times_et)
    assert isinstance(e.pass1_top_n, int)


def test_deadman_section_types(config):
    d = config.deadman
    assert isinstance(d.scan_timeout_minutes, int)
    assert isinstance(d.widen_stops_minutes, int)
    assert isinstance(d.force_close_minutes, int)


def test_ml_section_types(config):
    m = config.ml
    assert isinstance(m.min_events_logistic, int)
    assert isinstance(m.lgbm_learning_rate, float)


# ---------------------------------------------------------------------------
# Default values match spec
# ---------------------------------------------------------------------------

def test_default_signal_weights(config):
    s = config.signal
    assert s.weight_volume == pytest.approx(0.30)
    assert s.weight_return == pytest.approx(0.25)
    assert s.weight_options == pytest.approx(0.15)
    assert s.weight_sector_rs == pytest.approx(0.10)
    assert s.weight_delay == pytest.approx(0.10)


def test_signal_weights_sum(config):
    """Default weights sum to 0.90 per whitepaper (engine normalises at runtime)."""
    s = config.signal
    total = s.weight_volume + s.weight_return + s.weight_options + s.weight_sector_rs + s.weight_delay
    assert total == pytest.approx(0.90, abs=0.01)


def test_default_scan_times(config):
    assert config.execution.scan_times_et == ["09:35", "12:00", "15:30"]


def test_default_account_type(config):
    assert config.tax.account_type == "roth_ira"


def test_default_volume_windows(config):
    assert config.signal.volume_windows == [20, 60, 120]


def test_default_phase1_risk(config):
    assert config.risk.phase1_risk_per_trade == pytest.approx(0.005)


# ---------------------------------------------------------------------------
# Dynamic get() access
# ---------------------------------------------------------------------------

def test_get_dotted_key(config):
    assert config.get("universe.market_cap_min_mm") == 500


def test_get_missing_key_returns_default(config):
    assert config.get("nonexistent.key") is None
    assert config.get("nonexistent.key", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------

def test_missing_required_key_raises(tmp_path, raw_config):
    """Removing a required key must raise ValueError on load."""
    bad = copy.deepcopy(raw_config)
    del bad["signal"]["weight_volume"]
    cfg_file = tmp_path / "bad_config.yaml"
    cfg_file.write_text(yaml.dump(bad))
    with pytest.raises(ValueError, match="Missing required key"):
        ConfigLoader(cfg_file)


def test_wrong_type_raises(tmp_path, raw_config):
    """Setting a float field to a string must raise ValueError."""
    bad = copy.deepcopy(raw_config)
    bad["universe"]["market_cap_min_mm"] = "not_a_number"
    cfg_file = tmp_path / "bad_config.yaml"
    cfg_file.write_text(yaml.dump(bad))
    with pytest.raises(ValueError, match="Type error"):
        ConfigLoader(cfg_file)


def test_weights_not_summing_to_one_raises(tmp_path, raw_config):
    """Signal weights that don't sum to 1.0 must raise ValueError."""
    bad = copy.deepcopy(raw_config)
    bad["signal"]["weight_volume"] = 0.99  # total will be >> 1.0
    cfg_file = tmp_path / "bad_config.yaml"
    cfg_file.write_text(yaml.dump(bad))
    with pytest.raises(ValueError, match="exceeds 1.0"):
        ConfigLoader(cfg_file)


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        ConfigLoader("/nonexistent/path/config.yaml")


# ---------------------------------------------------------------------------
# Config diff
# ---------------------------------------------------------------------------

def test_diff_detects_changed_value(config, raw_config):
    old = copy.deepcopy(raw_config)
    old["universe"]["market_cap_min_mm"] = 300  # was 500
    changes = config.diff(old)
    keys = [c[0] for c in changes]
    assert "universe.market_cap_min_mm" in keys
    # Find the specific change
    change = next(c for c in changes if c[0] == "universe.market_cap_min_mm")
    assert change[1] == 300
    assert change[2] == 500


def test_diff_empty_when_no_changes(config, raw_config):
    changes = config.diff(raw_config)
    assert changes == []
