"""Database engine factory for native SQLite and Docker PostgreSQL runtimes."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus

import yaml
from sqlalchemy import create_engine, event


DATABASE_URL_ENV = "VISION_OFFICE_DATABASE_URL"


def _database_config(path: str | Path = "config/settings.yaml") -> dict:
    config_path = Path(path)
    if not config_path.is_file():
        return {}
    with config_path.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    values = config.get("database") or {}
    return values if isinstance(values, dict) else {}


def database_url(path: str | Path = "config/settings.yaml") -> str:
    """Resolve the database URL without ever logging credentials."""
    override = os.getenv(DATABASE_URL_ENV, "").strip()
    if override:
        return override

    config = _database_config(path)
    db_type = str(config.get("type") or "sqlite").strip().lower()
    if db_type in {"postgres", "postgresql"}:
        explicit_url = str(config.get("url") or "").strip()
        if explicit_url:
            return explicit_url
        host = str(config.get("host") or "127.0.0.1").strip()
        port = int(config.get("port") or 5432)
        name = str(config.get("name") or "vision_office").strip()
        user = str(config.get("user") or "vision_office").strip()
        password = str(config.get("password") or "").strip()
        if not password:
            raise ValueError("PostgreSQL password is required in database.password or VISION_OFFICE_DATABASE_URL")
        return f"postgresql+psycopg://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{quote_plus(name)}"

    db_path = str(config.get("path") or "data/office.db")
    return f"sqlite:///{db_path}"


def get_engine(url: str | None = None):
    resolved_url = url or database_url()
    if resolved_url.startswith("sqlite:///"):
        db_path = resolved_url.removeprefix("sqlite:///")
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        engine = create_engine(
            resolved_url,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _configure_sqlite(connection, _connection_record):
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()

        return engine

    return create_engine(resolved_url, pool_pre_ping=True, pool_recycle=1800)


def init_db():
    engine = get_engine()
    from database.migrations import run_migrations

    run_migrations(engine)
    print("Database schema is ready.")


if __name__ == "__main__":
    init_db()
