"""Device-local camera overrides; integration settings remain in the base YAML."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path


CONTROL_FILE = Path(__file__).resolve().parents[1] / "data" / "camera_controls.json"
PROFILES = {"configured": "Из settings.yaml", "balanced": "Баланс", "detail": "Детализация", "economy": "Экономичный"}
_WRITE_LOCK = threading.Lock()


def read_controls(path: Path = CONTROL_FILE):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, ""
    try:
        value = json.loads(raw)
        if value.get("version") != 1 or not isinstance(value.get("cameras"), dict):
            raise ValueError()
        return value["cameras"], hashlib.sha256(raw).hexdigest()
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("Файл управления камерами повреждён; восстановите data/camera_controls.json из резервной копии.") from error


def merge_cameras(base, overrides):
    from core.config import CameraSettings, ConfigurationError, _camera_url

    merged = {camera.id: asdict(camera) for camera in base}
    allowed = set(CameraSettings.__dataclass_fields__)
    for camera_id, values in overrides.items():
        if not isinstance(values, dict) or set(values) - allowed or values.get("id") != camera_id:
            raise ConfigurationError("Invalid camera override")
        merged[camera_id] = {**merged.get(camera_id, {}), **values}
    result = []
    for index, values in enumerate(merged.values()):
        camera = CameraSettings(**values)
        if (camera.event_type not in {"entry", "exit"} or camera.profile not in PROFILES
                or not isinstance(camera.is_active, bool) or not isinstance(camera.restart_token, int)):
            raise ConfigurationError("Invalid camera direction, profile or enabled state")
        _camera_url(camera.rtsp_url, index)
        if not isinstance(camera.device_id, str) or len(camera.device_id) > 64 or camera.device_id != camera.device_id.strip():
            raise ConfigurationError("Invalid camera deviceId")
        result.append(camera)
    return tuple(result)


def merge_device_ids(base, overrides):
    """Append camera routes without reassigning existing attendance identities."""
    from core.config import ConfigurationError

    devices = dict(base)
    for camera_id, values in overrides.items():
        if not isinstance(values, dict) or values.get("id") != camera_id:
            raise ConfigurationError("Invalid camera override")
        device_id = values.get("device_id", "")
        if not isinstance(device_id, str) or len(device_id) > 64 or device_id != device_id.strip():
            raise ConfigurationError("deviceId: не более 64 символов, без пробелов по краям.")
        if not device_id:
            continue
        if camera_id in devices and devices[camera_id] != device_id:
            raise ConfigurationError("Нельзя изменить существующий deviceId камеры.")
        devices[camera_id] = device_id
    if len(set(devices.values())) != len(devices):
        raise ConfigurationError("Этот deviceId уже назначен другой камере.")
    return devices


def save_camera(camera, *, expected_revision: str, path: Path = CONTROL_FILE, base_device_ids=None):
    from core.config import ConfigurationError

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", camera.id):
        raise ConfigurationError("ID камеры: латинские буквы, цифры, дефис или подчёркивание, до 100 символов.")
    if not camera.name.strip() or len(camera.name) > 100 or len(camera.location) > 200:
        raise ConfigurationError("Укажите название до 100 символов и расположение до 200 символов.")
    merge_cameras((), {camera.id: asdict(camera)})
    with _WRITE_LOCK:
        current, revision = read_controls(path)
        if revision != expected_revision:
            raise ConfigurationError("Настройки изменены в другой сессии. Обновите страницу и повторите сохранение.")
        previous_device = current.get(camera.id, {}).get("device_id", "")
        if previous_device and camera.device_id != previous_device:
            raise ConfigurationError("Нельзя изменить существующий deviceId камеры.")
        current[camera.id] = asdict(camera)
        merge_device_ids(base_device_ids or {}, current)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".camera-controls-", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "cameras": current}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def camera_ai_settings(camera, base):
    settings = dict(base)
    presets = {
        "balanced": {"face_detection_imgsz": 640, "face_detection_fps": 12},
        "detail": {"face_detection_imgsz": 960, "face_detection_fps": 15},
        "economy": {"face_detection_imgsz": 640, "face_detection_fps": 6},
    }
    settings.update(presets.get(camera.profile, {}))
    return settings
