from sqlalchemy import create_engine
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
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

def init_db():
    engine = get_engine()
    from database.migrations import run_migrations

    run_migrations(engine)
    print("Database schema is ready.")

if __name__ == "__main__":
    init_db()
