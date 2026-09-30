"""
Alembic environment configuration for Arconian.

Reads DB_PATH from the environment (falls back to arconian.db) so that
the same migrations work for dev, test, and prod databases.

Uses SQLAlchemy metadata from models.py for autogenerate support —
run `alembic revision --autogenerate -m "description"` to generate
migration files from model changes.
"""

import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure project root is on sys.path so models.py can be imported.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Base  # noqa: E402

# ---------------------------------------------------------------------------
# Alembic Config object (gives access to alembic.ini values)
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")

# Use Base.metadata for autogenerate
target_metadata = Base.metadata


def get_url() -> str:
    """Return DB URL from environment, falling back to alembic.ini value."""
    db_path = os.environ.get("DB_PATH")
    if db_path:
        return f"sqlite:///{db_path}"
    # Fall back to alembic.ini sqlalchemy.url
    return config.get_main_option("sqlalchemy.url", "sqlite:///arconian.db")


# ---------------------------------------------------------------------------
# Offline migrations (generate SQL without a live connection)
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # Required for SQLite ALTER TABLE support
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migrations (run against a live connection)
# ---------------------------------------------------------------------------
def run_migrations_online() -> None:
    # Import here to avoid circular imports at module load time
    from db import get_engine

    connectable = get_engine(db_url=get_url())

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # Required for SQLite ALTER TABLE support
        )
        with context.begin_transaction():
            context.run_migrations()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
