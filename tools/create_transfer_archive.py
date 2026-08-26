"""Create a portable project archive without reading the live SQLite file directly."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = PROJECT_ROOT / "acs2-transfer.zip"
SKIPPED_DIRECTORIES = {".venv", ".git", "__pycache__", ".pytest_cache"}
SKIPPED_FILES = {"acs2.zip", "acs2-transfer.zip", "acs2-update.zip"}


def should_skip(path: Path) -> bool:
    relative_path = path.relative_to(PROJECT_ROOT)
    return (
        any(part in SKIPPED_DIRECTORIES for part in relative_path.parts)
        or path.name in SKIPPED_FILES
        or path.suffix in {".pyc", ".zip"}
    )


def copy_database_snapshot(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as live_database:
        with sqlite3.connect(destination) as snapshot_database:
            live_database.backup(snapshot_database)


def create_archive(destination: Path) -> Path:
    database_path = PROJECT_ROOT / "data" / "office.db"
    if not database_path.is_file():
        raise FileNotFoundError(f"Database file was not found: {database_path}")

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="acs2-export-") as temporary_directory:
        database_snapshot = Path(temporary_directory) / "office.db"
        copy_database_snapshot(database_path, database_snapshot)

        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in PROJECT_ROOT.rglob("*"):
                if not path.is_file() or should_skip(path) or path == database_path:
                    continue
                archive.write(path, path.relative_to(PROJECT_ROOT))
            archive.write(database_snapshot, Path("data") / "office.db")

    return destination


if __name__ == "__main__":
    archive_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ARCHIVE
    result = create_archive(archive_path)
    print(f"Archive created: {result}")
    print("Database included as a consistent SQLite snapshot.")
