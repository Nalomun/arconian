"""
Database connection factory for Arconian.

Provides a singleton SQLite engine configured for WAL mode with
busy_timeout=5000ms and synchronous=NORMAL. All modules should obtain
sessions via get_session() rather than creating their own connections.

WAL mode allows Zinniinae (the companion Streamlit app) to read
concurrently without blocking Arconian's writes.

Whitepaper reference: Section 7 (Data Persistence)
Supplements reference: Doc 3, Section 3.1 (schema overview)
"""

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_db_url() -> str:
    """Return SQLite URL from DB_PATH env var, defaulting to arconian.db."""
    db_path = os.environ.get("DB_PATH", "arconian.db")
    return f"sqlite:///{db_path}"


def _build_engine(url: str) -> Engine:
    """Create and configure a SQLAlchemy engine with Arconian's SQLite pragmas."""
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def get_engine(db_url: str | None = None) -> Engine:
    """
    Return the singleton database engine, creating it on first call.

    Args:
        db_url: Override URL (mainly for testing with in-memory DBs).

    Returns:
        Configured SQLAlchemy Engine.
    """
    global _engine
    if _engine is None:
        url = db_url or get_db_url()
        _engine = _build_engine(url)
        logger.info("Database engine initialised: %s", url)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the singleton session factory."""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=True,
            autocommit=False,
        )
    return _SessionFactory


def get_session() -> Session:
    """
    Return a new SQLAlchemy session.

    Caller is responsible for committing and closing. Use the
    session_scope() context manager for automatic cleanup.
    """
    return get_session_factory()()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """
    Context manager for a database session with automatic commit/rollback.

    Usage:
        with session_scope() as session:
            session.add(record)

    Commits on clean exit, rolls back on exception.
    """
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _apply_migrations(engine) -> None:
    """
    Idempotent column-level migrations for tables that already exist.

    create_all() only creates missing tables; it never alters existing ones.
    New columns added after initial deploy are handled here via ALTER TABLE.
    """
    with engine.connect() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(trades)"))}
        new_cols = [
            ("tp1_hit",             "INTEGER NOT NULL DEFAULT 0"),
            ("shares_remaining",    "REAL"),
            ("trailing_stop_price", "REAL"),
            ("tp1_realized_pnl",    "REAL"),
        ]
        for col, defn in new_cols:
            if col not in existing:
                conn.execute(text(f"ALTER TABLE trades ADD COLUMN {col} {defn}"))
                logger.info("Migration: added trades.%s", col)
        conn.commit()


def init_db() -> None:
    """
    Create all tables defined in models.py if they do not already exist,
    then apply any pending column-level migrations.

    Called once on startup before the scheduler begins. Idempotent —
    safe to call on a DB that already has the schema.
    """
    from models import Base  # local import avoids circular deps at module load

    Base.metadata.create_all(get_engine())
    _apply_migrations(get_engine())
    logger.info("Database schema verified / initialised.")


def reset_engine_for_testing(db_url: str = "sqlite:///:memory:") -> None:
    """
    Replace the singleton engine with a fresh in-memory instance.

    FOR TESTING ONLY. Resets both the engine and session factory singletons.

    Args:
        db_url: In-memory or temp-file URL to use for tests.
    """
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = _build_engine(db_url)
    _SessionFactory = None
    logger.debug("Engine reset for testing: %s", db_url)
