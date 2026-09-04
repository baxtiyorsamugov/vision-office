"""Small dependency-free health probes used by Docker Compose."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _http_ok(url: str) -> bool:
    try:
        with urlopen(url, timeout=3) as response:
            return 200 <= response.status < 300
    except OSError:
        return False


def _worker_ok() -> bool:
    try:
        import yaml

        config = yaml.safe_load((PROJECT_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
        active_ids = {
            str(camera.get("id"))
            for camera in config.get("cameras", [])
            if isinstance(camera, dict) and camera.get("is_active", True)
        }
        statuses = {}
        for status_file in (PROJECT_ROOT / "data").glob("runtime_status_*.json"):
            value = json.loads(status_file.read_text(encoding="utf-8"))
            statuses[str(value.get("camera_id"))] = value
        return bool(active_ids) and all(statuses.get(camera_id, {}).get("running") for camera_id in active_ids)
    except (OSError, ValueError, TypeError):
        return False


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    if target == "api":
        return 0 if _http_ok("http://127.0.0.1:8000/api/v1/health") else 1
    if target == "ui":
        return 0 if _http_ok("http://127.0.0.1:8501/_stcore/health") else 1
    if target == "worker":
        return 0 if _worker_ok() else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
