"""Create a code-only update archive that preserves target computer data."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = PROJECT_ROOT / "acs2-update.zip"
SKIPPED_DIRECTORIES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "data",
    "models",
}
SKIPPED_FILES = {
    ".env",
    "acs2-transfer.zip",
    "acs2-update.zip",
    "settings.yaml",
    "yolov8n-face.pt",
}


def should_skip(path: Path) -> bool:
    relative_path = path.relative_to(PROJECT_ROOT)
    return (
        any(part in SKIPPED_DIRECTORIES for part in relative_path.parts)
        or path.name in SKIPPED_FILES
        or path.suffix in {".onnx", ".pt", ".pyc", ".zip"}
    )


def create_update_archive(destination: Path) -> Path:
    destination = destination.resolve()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in PROJECT_ROOT.rglob("*"):
            if path.is_file() and not should_skip(path):
                archive.write(path, path.relative_to(PROJECT_ROOT))
    return destination


if __name__ == "__main__":
    archive_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ARCHIVE
    result = create_update_archive(archive_path)
    print(f"Update archive created: {result}")
    print("Database, faces, RTSP settings, and models were excluded.")
