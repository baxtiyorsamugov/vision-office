"""Small runtime-status file shared between the camera process and Streamlit UI."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATUS_FILE = PROJECT_ROOT / "data" / "runtime_status.json"


def write_runtime_status(status: dict[str, Any]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, STATUS_FILE)


def _camera_status_file(camera_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", camera_id)[:100]
    return PROJECT_ROOT / "data" / f"runtime_status_{safe_id}.json"


def write_camera_runtime_status(camera_id: str, status: dict[str, Any]) -> None:
    """Each camera owns a file, so independent processes never overwrite it."""
    destination = _camera_status_file(camera_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps({"camera_id": camera_id, **status}, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, destination)


def read_runtime_status() -> dict[str, Any] | None:
    camera_statuses = []
    for path in STATUS_FILE.parent.glob("runtime_status_*.json"):
        try:
            camera_statuses.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    if camera_statuses:
        return {
            "running": any(status.get("running") for status in camera_statuses),
            "cameras": sorted(camera_statuses, key=lambda item: str(item.get("camera_id"))),
        }
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
