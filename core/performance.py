"""Small runtime-status file shared between the camera process and Streamlit UI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATUS_FILE = PROJECT_ROOT / "data" / "runtime_status.json"


def write_runtime_status(status: dict[str, Any]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, STATUS_FILE)


def read_runtime_status() -> dict[str, Any] | None:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
