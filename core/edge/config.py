from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


@dataclass(frozen=True)
class EdgeSettings:
    enabled: bool = False
    base_url: str = ""
    device_api_key: str = ""
    device_id: str = ""
    sync_interval_seconds: int = 300
    full_sync_interval_seconds: int = 86400
    page_size: int = 200
    recognition_threshold: float = 0.40
    event_cooldown_seconds: int = 60
    request_timeout_seconds: int = 10
    timezone: str = "Asia/Tashkent"

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.base_url and self.device_api_key and self.device_id)

    def validation_error(self) -> str | None:
        if not self.enabled:
            return None
        missing = [name for name, value in (
            ("base_url", self.base_url),
            ("device_api_key", self.device_api_key),
            ("device_id", self.device_id),
        ) if not value or str(value).startswith("REPLACE_")]
        if missing:
            return f"edge_integration requires: {', '.join(missing)}"
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "edge_integration.base_url must be a valid HTTP or HTTPS URL"
        return None


def load_edge_settings(path: str | Path = "config/settings.yaml") -> EdgeSettings:
    config_path = Path(path)
    if not config_path.is_file():
        return EdgeSettings()
    with config_path.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    values = config.get("edge_integration") or {}
    defaults = EdgeSettings()
    return EdgeSettings(
        enabled=bool(values.get("enabled", defaults.enabled)),
        base_url=str(values.get("base_url", defaults.base_url)).rstrip("/"),
        device_api_key=str(values.get("device_api_key", defaults.device_api_key)),
        device_id=str(values.get("device_id", defaults.device_id)),
        sync_interval_seconds=int(values.get("sync_interval_seconds", defaults.sync_interval_seconds)),
        full_sync_interval_seconds=int(values.get("full_sync_interval_seconds", defaults.full_sync_interval_seconds)),
        page_size=min(1000, max(1, int(values.get("page_size", defaults.page_size)))),
        recognition_threshold=float(values.get("recognition_threshold", defaults.recognition_threshold)),
        event_cooldown_seconds=max(0, int(values.get("event_cooldown_seconds", defaults.event_cooldown_seconds))),
        request_timeout_seconds=max(1, int(values.get("request_timeout_seconds", defaults.request_timeout_seconds))),
        timezone=str(values.get("timezone", defaults.timezone)),
    )
