"""Small runtime-status file shared between the camera process and Streamlit UI."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATUS_FILE = PROJECT_ROOT / "data" / "runtime_status.json"


def _write_status_file(destination: Path, status: dict[str, Any]) -> bool:
    """Best-effort atomic status update that cannot stop a camera on Windows."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(status, ensure_ascii=False)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(payload, encoding="utf-8")
        # Antivirus, Streamlit or Explorer may briefly hold the destination open.
        for attempt in range(5):
            try:
                os.replace(temporary, destination)
                return True
            except PermissionError:
                if attempt == 4:
                    return False
                time.sleep(0.05 * (attempt + 1))
    except OSError:
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_runtime_status(status: dict[str, Any]) -> bool:
    return _write_status_file(STATUS_FILE, status)


def _camera_status_file(camera_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", camera_id)[:100]
    return PROJECT_ROOT / "data" / f"runtime_status_{safe_id}.json"


def write_camera_runtime_status(camera_id: str, status: dict[str, Any]) -> bool:
    """Each camera owns a file, so independent processes never overwrite it."""
    destination = _camera_status_file(camera_id)
    return _write_status_file(destination, {"camera_id": camera_id, **status})


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
