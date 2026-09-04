from sqlalchemy import create_engine
from sqlalchemy import event
from database.models import Base
import yaml
import os


def _database_path() -> str:
    with open("config/settings.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    return str((config.get("database") or {}).get("path") or "data/office.db")

def get_engine():
    db_path = _database_path()
    # Создаем папку data, если её нет
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(connection, _connection_record):
        # API, UI and recognition workers share one durable SQLite file in Docker.
        # WAL plus a busy timeout avoids transient write-lock failures between them.
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    return engine

def init_db():
    engine = get_engine()
    from database.migrations import run_migrations

    run_migrations(engine)
    print("Database schema is ready.")

if __name__ == "__main__":
    init_db()
