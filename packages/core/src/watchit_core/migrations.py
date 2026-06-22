from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from watchit_core.config import settings


def _sqlalchemy_database_url(database_url: str | None = None) -> str:
    url = database_url or settings.database_url
    if not url:
        raise RuntimeError("DATABASE_URL is required to run migrations")
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def run_migrations(database_url: str | None = None) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    config.set_main_option("sqlalchemy.url", _sqlalchemy_database_url(database_url))
    command.upgrade(config, "head")
