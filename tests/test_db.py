"""Tests for db.py — connection factory and SQLite pragma configuration."""

import pytest
from sqlalchemy import text

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import get_engine, get_session, session_scope, init_db, reset_engine_for_testing
from models import Base, UniverseState, Override
from timeutils import utcnow


@pytest.fixture(autouse=True)
def isolated_db():
    """Reset to a fresh in-memory DB before each test."""
    reset_engine_for_testing("sqlite:///:memory:")
    yield
    reset_engine_for_testing("sqlite:///:memory:")


def test_wal_mode_is_set(tmp_path):
    """journal_mode=WAL pragma must be applied on every new connection.

    In-memory SQLite does not support WAL, so this test uses a temp file.
    """
    db_file = tmp_path / "wal_test.db"
    reset_engine_for_testing(f"sqlite:///{db_file}")
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA journal_mode")).fetchone()
    assert result[0] == "wal"


def test_foreign_keys_are_enabled():
    """foreign_keys pragma must be ON."""
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA foreign_keys")).fetchone()
    assert result[0] == 1


def test_synchronous_normal():
    """synchronous pragma must be NORMAL (1)."""
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("PRAGMA synchronous")).fetchone()
    assert result[0] == 1  # NORMAL = 1


def test_init_db_creates_tables():
    """init_db() must create all tables without error."""
    init_db()
    engine = get_engine()
    from sqlalchemy import inspect
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    expected = {
        "parameter_history", "universe_state", "overrides",
        "ic_history", "trades", "signal_log", "outcome_prices",
    }
    assert expected.issubset(set(tables)), f"Missing tables: {expected - set(tables)}"


def test_session_scope_commits_on_success():
    """Records written inside session_scope() are visible after the block."""
    init_db()
    from datetime import datetime

    with session_scope() as session:
        state = UniverseState(
            ticker="AAPL",
            state="CANDIDATE",
            state_since=utcnow(),
        )
        session.add(state)

    # Verify it persists in a new session
    with session_scope() as session:
        result = session.get(UniverseState, "AAPL")
    assert result is not None
    assert result.state == "CANDIDATE"


def test_session_scope_rolls_back_on_exception():
    """Records written inside session_scope() are NOT committed if an exception is raised."""
    init_db()
    from datetime import datetime

    with pytest.raises(RuntimeError):
        with session_scope() as session:
            state = UniverseState(
                ticker="TSLA",
                state="ACTIVE",
                state_since=utcnow(),
            )
            session.add(state)
            raise RuntimeError("simulated failure")

    with session_scope() as session:
        result = session.get(UniverseState, "TSLA")
    assert result is None


def test_get_session_returns_usable_session():
    """get_session() returns a session that can execute queries."""
    init_db()
    session = get_session()
    try:
        result = session.execute(text("SELECT 1")).fetchone()
        assert result[0] == 1
    finally:
        session.close()
