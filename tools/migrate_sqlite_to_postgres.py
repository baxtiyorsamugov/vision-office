"""One-time, explicit migration of an existing Vision Office SQLite database."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import MetaData, create_engine, select, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.manager import database_url
from database.migrations import run_migrations
from database.models import Base


def migrate(source_path: Path, target_url: str, replace: bool = False) -> dict[str, int]:
    if not source_path.is_file():
        raise ValueError(f"SQLite source database does not exist: {source_path}")
    target = create_engine(target_url, pool_pre_ping=True)
    if target.dialect.name != "postgresql":
        raise ValueError("Target must be a PostgreSQL URL")
    source = create_engine(f"sqlite:///{source_path.resolve()}")
    try:
        run_migrations(target)
        source_metadata = MetaData()
        source_metadata.reflect(bind=source)
        target_metadata = MetaData()
        target_metadata.reflect(bind=target)
        table_names = [table.name for table in Base.metadata.sorted_tables if table.name in source_metadata.tables]
        copied: dict[str, int] = {}
        with target.begin() as destination:
            populated = [
                table_name for table_name in table_names
                if destination.execute(select(target_metadata.tables[table_name]).limit(1)).first() is not None
            ]
            if populated and not replace:
                raise ValueError(
                    "PostgreSQL already contains data in: " + ", ".join(populated) + ". Use --replace only after a backup."
                )
            if replace:
                for table_name in reversed(table_names):
                    destination.execute(target_metadata.tables[table_name].delete())
            for table_name in table_names:
                source_table = source_metadata.tables[table_name]
                target_table = target_metadata.tables[table_name]
                with source.connect() as source_connection:
                    rows = source_connection.execute(select(source_table)).mappings().all()
                if rows:
                    destination.execute(target_table.insert(), [dict(row) for row in rows])
                copied[table_name] = len(rows)
                primary_keys = list(target_table.primary_key.columns)
                if rows and len(primary_keys) == 1:
                    column = primary_keys[0]
                    try:
                        is_integer_key = column.type.python_type is int
                    except NotImplementedError:
                        is_integer_key = False
                    if is_integer_key:
                        quote = target.dialect.identifier_preparer.quote
                        destination.execute(text(
                            f"SELECT setval(pg_get_serial_sequence('{target_table.name}', '{column.name}'), "
                            f"(SELECT MAX({quote(column.name)}) FROM {quote(target_table.name)}), true)"
                        ))
        return copied
    finally:
        source.dispose()
        target.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="data/office.db", help="Path to the existing SQLite office.db")
    parser.add_argument("--target-url", default=None, help="PostgreSQL URL; defaults to VISION_OFFICE_DATABASE_URL")
    parser.add_argument("--replace", action="store_true", help="Replace existing PostgreSQL application data after making a backup")
    args = parser.parse_args()
    target_url = args.target_url or database_url()
    copied = migrate(Path(args.source), target_url, replace=args.replace)
    print("SQLite data copied to PostgreSQL:", ", ".join(f"{table}={count}" for table, count in copied.items()))


if __name__ == "__main__":
    main()
