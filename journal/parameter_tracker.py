"""
Parameter Tracker — config change audit log.

On startup, compares the current config hash against the most-recently
recorded hash in parameter_history. If different, diffs the two raw
configs and writes each changed parameter as a row in parameter_history.

Also called by the IC recalibration task when weights are updated
(triggered_by='ic_recalibration').

Supplements reference: Doc 2 (Configuration Parameter Registry)
"""

import json
import logging
from datetime import datetime
from timeutils import utcnow
from typing import Callable, Any

logger = logging.getLogger(__name__)


def record_config_snapshot(config, session_scope: Callable) -> int:
    """
    Compare current config against the last snapshot; write any changes.

    Args:
        config: Loaded ConfigLoader instance.
        session_scope: Callable context manager returning a SQLAlchemy session.

    Returns:
        Number of changed parameters written (0 if unchanged or first run).
    """
    from models import ParameterHistory

    # The comparison baseline must be a FULL-CONFIG snapshot row
    # (parameter_path == "_baseline"), never a per-key change row. Change rows
    # store a scalar in new_value; diffing the full config against a scalar
    # produced a garbage row with parameter_path="" (F-23). Filtering to
    # "_baseline" — with id as the tie-break for same-timestamp rows — makes
    # the diff base deterministic and always parseable as a dict.
    try:
        with session_scope() as session:
            last = (
                session.query(ParameterHistory)
                .filter_by(triggered_by="startup", parameter_path="_baseline")
                .order_by(
                    ParameterHistory.timestamp.desc(),
                    ParameterHistory.id.desc(),
                )
                .first()
            )
            last_raw = last.new_value if last is not None else None
    except Exception:
        logger.warning("parameter_tracker: failed to query history", exc_info=True)
        return 0

    now = utcnow()
    written = 0

    if last_raw is None:
        # First run — record the baseline so the next startup has a point of
        # comparison. Nothing to diff yet.
        _write_baseline(config, session_scope, now)
        return 0

    # Compare current raw config against the stored full-config snapshot.
    try:
        stored_raw = json.loads(last_raw)
    except (ValueError, TypeError):
        logger.warning("parameter_tracker: could not parse stored baseline — re-baselining")
        # Don't diff against an unparseable baseline; refresh it so we recover.
        _write_baseline(config, session_scope, now)
        return 0

    changes = config.diff(stored_raw)
    if changes:
        logger.info("parameter_tracker: %d config change(s) detected", len(changes))
        try:
            with session_scope() as session:
                for dotted_key, old_val, new_val in changes:
                    row = ParameterHistory(
                        timestamp=now,
                        parameter_path=dotted_key,
                        old_value=_serialise(old_val),
                        new_value=_serialise(new_val),
                        reason="startup config diff",
                        triggered_by="startup",
                    )
                    session.add(row)
                    written += 1
        except Exception:
            logger.error("parameter_tracker: failed to write changes", exc_info=True)
    else:
        logger.debug("parameter_tracker: config unchanged (hash %s)", config.config_hash[:12])

    # Always refresh the full-config baseline so the NEXT startup diffs against
    # the current config — never against a stale baseline or a scalar change row.
    _write_baseline(config, session_scope, now)
    return written


def record_weight_update(
    old_weights: dict[str, float],
    new_weights: dict[str, float],
    reason: str,
    session_scope: Callable,
) -> int:
    """
    Write signal weight changes to parameter_history after IC recalibration.

    Args:
        old_weights: Dict of {signal_component: old_weight}.
        new_weights: Dict of {signal_component: new_weight}.
        reason: Description, e.g. 'IC recalibration period 3'.
        session_scope: Callable context manager for a session.

    Returns:
        Number of rows written.
    """
    from models import ParameterHistory

    now = utcnow()
    written = 0

    try:
        with session_scope() as session:
            for key in set(old_weights) | set(new_weights):
                old_v = old_weights.get(key)
                new_v = new_weights.get(key)
                if old_v == new_v:
                    continue
                row = ParameterHistory(
                    timestamp=now,
                    parameter_path=f"signal.weight_{key}",
                    old_value=_serialise(old_v),
                    new_value=_serialise(new_v),
                    reason=reason,
                    triggered_by="ic_recalibration",
                )
                session.add(row)
                written += 1
    except Exception:
        logger.error("parameter_tracker: weight update write failed", exc_info=True)

    return written


def _write_baseline(config, session_scope: Callable, now: datetime) -> None:
    """Write a single baseline row capturing the full raw config as JSON."""
    from models import ParameterHistory
    try:
        with session_scope() as session:
            row = ParameterHistory(
                timestamp=now,
                parameter_path="_baseline",
                old_value="{}",
                new_value=json.dumps(config.raw, default=str),
                reason="initial baseline",
                triggered_by="startup",
            )
            session.add(row)
        logger.info("parameter_tracker: baseline snapshot written")
    except Exception:
        logger.warning("parameter_tracker: failed to write baseline", exc_info=True)


def _serialise(value: Any) -> str:
    """Convert any config value to a JSON string."""
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)
