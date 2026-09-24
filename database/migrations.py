"""Small additive migration runner for installed Vision Office devices."""

from __future__ import annotations

from sqlalchemy import inspect, text

from database.models import Base


MIGRATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "20260903_edge_event_columns",
        (
            "ALTER TABLE remote_persons ADD COLUMN photo_url VARCHAR(1024)",
            "ALTER TABLE remote_persons ADD COLUMN photo_path VARCHAR(1024)",
            "ALTER TABLE remote_persons ADD COLUMN embedding_status VARCHAR(32) NOT NULL DEFAULT 'pending'",
            "ALTER TABLE remote_persons ADD COLUMN embedding_error TEXT",
            "ALTER TABLE access_log_outbox ADD COLUMN endpoint VARCHAR(255) NOT NULL DEFAULT '/api/v1/learning-centers/access-logs'",
        ),
    ),
    (
        "20260908_manual_full_erp_sync",
        (
            "ALTER TABLE edge_sync_state ADD COLUMN manual_full_sync_requested_at TIMESTAMP",
            "ALTER TABLE edge_sync_state ADD COLUMN last_manual_full_sync_at TIMESTAMP",
        ),
    ),
    (
        "20260924_eduschool_source_faceid",
        (
            "ALTER TABLE eduschool_catalog_people ADD COLUMN source_photo_status VARCHAR(20) NOT NULL DEFAULT 'pending'",
            "ALTER TABLE eduschool_catalog_people ADD COLUMN source_photo_error TEXT",
            "ALTER TABLE eduschool_catalog_people ADD COLUMN source_photo_retry_at TIMESTAMP",
            "ALTER TABLE eduschool_reference_photos ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'local'",
            "ALTER TABLE eduschool_reference_photos ADD COLUMN source_url VARCHAR(1024)",
        ),
    ),
    (
        "20260924_eduschool_turnstile_people",
        (
            "ALTER TABLE eduschool_catalog_people ADD COLUMN employee_no VARCHAR(64)",
            "ALTER TABLE eduschool_catalog_people ADD COLUMN attendance_approved BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE eduschool_catalog_people ADD COLUMN attendance_approved_at TIMESTAMP WITH TIME ZONE",
        ),
    ),
    (
        "20260924_eduschool_turnstile_auto_qualification",
        (
            "ALTER TABLE eduschool_catalog_people ADD COLUMN attendance_blocked BOOLEAN NOT NULL DEFAULT FALSE",
        ),
    ),
)


def _ensure_migration_table(connection) -> None:
    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    ))


def _column_exists(connection, table: str, column: str) -> bool:
    return column in {item["name"] for item in inspect(connection).get_columns(table)}


def _is_duplicate_column(error: Exception) -> bool:
    return "duplicate column name" in str(error).lower()


def run_migrations(engine) -> list[str]:
    """Create missing tables and safely apply each additive migration exactly once."""
    Base.metadata.create_all(engine)
    applied: list[str] = []
    with engine.begin() as connection:
        _ensure_migration_table(connection)
        existing = {
            row[0] for row in connection.execute(text("SELECT version FROM schema_migrations"))
        }
        for version, statements in MIGRATIONS:
            if version in existing:
                continue
            for statement in statements:
                parts = statement.split()
                table, column = parts[2], parts[5]
                if _column_exists(connection, table, column):
                    continue
                try:
                    connection.execute(text(statement))
                except Exception as error:
                    if not _is_duplicate_column(error):
                        raise
            connection.execute(text("INSERT INTO schema_migrations(version) VALUES (:version)"), {"version": version})
            applied.append(version)
    return applied
