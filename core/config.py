"""Validated application configuration shared by live and supervisor modes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


class ConfigurationError(ValueError):
    """Raised before workers start when local device configuration is invalid."""


@dataclass(frozen=True)
class CameraSettings:
    id: str
    rtsp_url: str
    is_active: bool = True
    event_type: str = "entry"
    name: str = "Camera"
    location: str = ""


@dataclass(frozen=True)
class AppSettings:
    cameras: tuple[CameraSettings, ...]
    ai: dict[str, Any]
    system: dict[str, Any]
    database_path: str
    log_level: str


def _camera_id(value: Any, index: int) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ConfigurationError(f"cameras[{index}].id is required")
    # Existing deployments use friendly ids. UUIDs are preferred for new cameras.
    if len(raw) > 100:
        raise ConfigurationError(f"cameras[{index}].id is too long")
    return raw


def _camera_url(value: Any, index: int) -> str:
    url = str(value or "").strip()
    if not url:
        raise ConfigurationError(f"cameras[{index}].rtsp_url is required")
    parsed = urlparse(url)
    if url != "0" and parsed.scheme not in {"rtsp", "http", "https", "file"}:
        raise ConfigurationError(
            f"cameras[{index}].rtsp_url must be an RTSP/HTTP URL, a file URL, or 0 for a USB camera"
        )
    return url


def load_app_settings(path: str | Path = "config/settings.yaml") -> AppSettings:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigurationError(
            f"Configuration file not found: {config_path}. Copy config/settings.example.yaml to config/settings.yaml."
        )
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream) or {}
    except yaml.YAMLError as error:
        raise ConfigurationError(f"Invalid YAML in {config_path}: {error}") from error

    cameras_raw = raw.get("cameras")
    if not isinstance(cameras_raw, list) or not cameras_raw:
        raise ConfigurationError("At least one camera is required in cameras")
    cameras: list[CameraSettings] = []
    ids: set[str] = set()
    for index, item in enumerate(cameras_raw):
        if not isinstance(item, dict):
            raise ConfigurationError(f"cameras[{index}] must be a YAML object")
        camera_id = _camera_id(item.get("id"), index)
        if camera_id in ids:
            raise ConfigurationError(f"Duplicate camera id: {camera_id}")
        ids.add(camera_id)
        event_type = str(item.get("event_type") or item.get("direction") or "entry").lower()
        if event_type not in {"entry", "exit"}:
            raise ConfigurationError(f"cameras[{index}].event_type must be entry or exit")
        cameras.append(CameraSettings(
            id=camera_id,
            rtsp_url=_camera_url(item.get("rtsp_url"), index),
            is_active=bool(item.get("is_active", True)),
            event_type=event_type,
            name=str(item.get("name") or camera_id),
            location=str(item.get("location") or ""),
        ))
    if not any(camera.is_active for camera in cameras):
        raise ConfigurationError("At least one camera must have is_active: true")

    database = raw.get("database") or {}
    database_path = str(database.get("path") or "").strip()
    if not database_path:
        raise ConfigurationError("database.path is required")
    system = raw.get("system") or {}
    log_level = str(system.get("log_level") or "INFO").upper()
    return AppSettings(
        cameras=tuple(cameras),
        ai=dict(raw.get("ai") or {}),
        system=dict(system),
        database_path=database_path,
        log_level=log_level,
    )
