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
    return _write_status_file(destination, {"camera_id": camera_id, **status, "updated_at": time.time()})


def _read_fresh_status(path: Path) -> dict[str, Any]:
    status = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(status, dict):
        raise ValueError("Invalid runtime status")
    age = max(0, time.time() - path.stat().st_mtime)
    status["heartbeat_age_seconds"] = round(age, 1)
    status["stale"] = age > 15
    if status["stale"]:
        status.update(running=False, ai_ready=False, stream_status="stale", capture_fps=0, detection_fps=0)
    return status


def configured_camera_statuses(settings=None) -> list[dict[str, Any]]:
    from core.config import load_app_settings
    settings = settings or load_app_settings()
    runtime = read_runtime_status() or {}
    items = runtime.get("cameras") or ([runtime] if runtime.get("camera_id") else [])
    by_id = {item.get("camera_id"): item for item in items}
    result = []
    for camera in settings.cameras:
        item = {"camera_id": camera.id, "running": False, "ai_ready": False,
                "stream_status": "waiting", **by_id.get(camera.id, {}),
                "camera_name": camera.name, "enabled": camera.is_active, "profile": camera.profile}
        if not camera.is_active:
            item.update(running=False, ai_ready=False, stream_status="disabled")
        result.append(item)
    return result


def read_runtime_status() -> dict[str, Any] | None:
    camera_statuses = []
    for path in STATUS_FILE.parent.glob("runtime_status_*.json"):
        try:
            camera_statuses.append(_read_fresh_status(path))
        except (OSError, ValueError, TypeError):
            continue
    if camera_statuses:
        return {
            "running": any(status.get("running") for status in camera_statuses),
            "cameras": sorted(camera_statuses, key=lambda item: str(item.get("camera_id"))),
        }
    try:
        return _read_fresh_status(STATUS_FILE)
    except (OSError, ValueError, TypeError):
        return None
