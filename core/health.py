"""Edge-device health monitoring, incident history and reliable Telegram delivery."""

from __future__ import annotations

import json
import logging
import shutil
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from core.edge.config import EdgeSettings, load_edge_settings
from core.edge.service import EdgeService
from core.events import RecognitionEventStore
from core.config import ConfigurationError, load_app_settings
from core.performance import PROJECT_ROOT, read_runtime_status
from database.manager import get_engine
from database.migrations import run_migrations
from database.models import EdgeSyncState, HealthIncident, NotificationOutbox


logger = logging.getLogger("vision_office.health")
HEALTH_FILE = PROJECT_ROOT / "data" / "health_status.json"


@dataclass(frozen=True)
class HealthSettings:
    enabled: bool = True
    check_interval_seconds: int = 5
    failure_threshold: int = 3
    heartbeat_interval_seconds: int = 60
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_ids: tuple[str, ...] = ()
    camera_stale_seconds: int = 15

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_enabled and self.telegram_bot_token and self.telegram_chat_ids)


def load_health_settings(path: str | Path = "config/settings.yaml") -> HealthSettings:
    config_path = Path(path)
    if not config_path.is_file():
        return HealthSettings(enabled=False)
    with config_path.open("r", encoding="utf-8") as stream:
        values = (yaml.safe_load(stream) or {}).get("health") or {}
    defaults = HealthSettings()
    ids = values.get("telegram_chat_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    return HealthSettings(
        enabled=bool(values.get("enabled", defaults.enabled)),
        check_interval_seconds=max(1, int(values.get("check_interval_seconds", defaults.check_interval_seconds))),
        failure_threshold=max(1, int(values.get("failure_threshold", defaults.failure_threshold))),
        heartbeat_interval_seconds=max(10, int(values.get("heartbeat_interval_seconds", defaults.heartbeat_interval_seconds))),
        telegram_enabled=bool(values.get("telegram_enabled", defaults.telegram_enabled)),
        telegram_bot_token=str(values.get("telegram_bot_token", "")),
        telegram_chat_ids=tuple(str(chat_id) for chat_id in ids if str(chat_id).strip()),
        camera_stale_seconds=max(3, int(values.get("camera_stale_seconds", defaults.camera_stale_seconds))),
    )


class HealthChecker:
    def __init__(self, settings: HealthSettings, edge_settings: EdgeSettings, engine=None):
        self.settings = settings
        self.edge_settings = edge_settings
        self.engine = engine or get_engine()
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.failure_counts: dict[str, int] = {}
        self.last_heartbeat_at = 0.0
        self.last_cleanup_at = 0.0
        self.edge_service = EdgeService(edge_settings, engine=self.engine) if edge_settings.configured else None

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.utcnow()

    def _database_status(self) -> tuple[str, str]:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return "ok", "SQLite is available"
        except Exception as error:
            return "failed", f"SQLite error: {error}"

    def _camera_statuses(self) -> dict[str, tuple[str, str]]:
        runtime = read_runtime_status() or {}
        cameras = runtime.get("cameras") or ([runtime] if runtime.get("camera_id") or runtime.get("running") else [])
        configured_ids = self._configured_camera_ids()
        if configured_ids is not None:
            cameras_by_id = {str(camera.get("camera_id")): camera for camera in cameras}
            cameras = [cameras_by_id[camera_id] for camera_id in configured_ids if camera_id in cameras_by_id]
            missing = configured_ids - set(cameras_by_id)
        else:
            missing = set()
        if not cameras and not missing:
            return {"application": ("failed", "No camera runtime status is available")}
        result: dict[str, tuple[str, str]] = {}
        for camera_id in sorted(missing):
            result[f"camera:{camera_id}"] = ("failed", "Camera worker has not published runtime status")
        for camera in cameras:
            camera_id = str(camera.get("camera_id") or "unknown")
            age = camera.get("frame_age_ms")
            connected = camera.get("stream_status") == "connected"
            running = bool(camera.get("running"))
            stale = age is None or float(age) > self.settings.camera_stale_seconds * 1000
            if running and connected and not stale:
                result[f"camera:{camera_id}"] = ("ok", "RTSP frames are current")
            else:
                result[f"camera:{camera_id}"] = ("failed", str(camera.get("stream_error") or "Camera is disconnected or frame is stale"))
        return result

    @staticmethod
    def _configured_camera_ids() -> set[str] | None:
        try:
            settings = load_app_settings()
        except ConfigurationError:
            return None
        return {camera.id for camera in settings.cameras if camera.is_active}

    def _edge_status(self) -> tuple[str, str]:
        if not self.edge_settings.enabled:
            return "ok", "ERP integration is disabled"
        if not self.edge_settings.configured:
            return "failed", "ERP integration is enabled but device credentials are incomplete"
        session = self.Session()
        try:
            state = session.get(EdgeSyncState, 1)
            if state and state.last_error:
                return "failed", state.last_error
            return "ok", "ERP synchronization has no recorded error"
        finally:
            session.close()

    @staticmethod
    def _disk_status() -> tuple[str, str]:
        try:
            usage = shutil.disk_usage(PROJECT_ROOT)
            percent = usage.free / usage.total * 100
            if percent < 5:
                return "failed", f"Free disk space is critically low: {percent:.1f}%"
            return "ok", f"Free disk space: {percent:.1f}%"
        except OSError as error:
            return "failed", f"Disk check failed: {error}"

    @staticmethod
    def _internet_status() -> tuple[str, str]:
        try:
            with socket.create_connection(("1.1.1.1", 53), timeout=2):
                return "ok", "Internet connectivity is available"
        except OSError:
            return "failed", "Internet connectivity check failed"

    def _queue_notification(self, message: str) -> None:
        if not self.settings.telegram_configured:
            return
        session = self.Session()
        try:
            for chat_id in self.settings.telegram_chat_ids:
                session.add(NotificationOutbox(
                    id=str(uuid.uuid4()),
                    channel="telegram",
                    payload=json.dumps({"chat_id": chat_id, "text": message}, ensure_ascii=False),
                    status="pending",
                    next_attempt_at=self._utcnow(),
                ))
            session.commit()
        finally:
            session.close()

    def _update_incident(self, component: str, status: str, message: str) -> None:
        session = self.Session()
        notification = None
        try:
            active = session.query(HealthIncident).filter(
                HealthIncident.component == component,
                HealthIncident.status == "open",
            ).order_by(HealthIncident.opened_at.desc()).first()
            if status == "ok":
                self.failure_counts.pop(component, None)
                if active:
                    active.status = "recovered"
                    active.recovered_at = self._utcnow()
                    notification = f"Vision Office recovered: {component}. {message}"
                session.commit()
            else:
                failures = self.failure_counts.get(component, 0) + 1
                self.failure_counts[component] = failures
                if failures >= self.settings.failure_threshold and not active:
                    incident = HealthIncident(
                        id=str(uuid.uuid4()), component=component, status="open", message=message,
                    )
                    session.add(incident)
                    notification = f"Vision Office alert: {component}. {message}"
                session.commit()
        finally:
            session.close()
        if notification:
            self._queue_notification(notification)

    def _deliver_notifications(self, limit: int = 10) -> int:
        if not self.settings.telegram_configured:
            return 0
        session = self.Session()
        try:
            due = session.query(NotificationOutbox).filter(
                NotificationOutbox.status.in_(["pending", "retry"]),
                NotificationOutbox.next_attempt_at <= self._utcnow(),
            ).order_by(NotificationOutbox.created_at.asc()).limit(limit).all()
            ids = [item.id for item in due]
        finally:
            session.close()
        sent = 0
        for item_id in ids:
            session = self.Session()
            try:
                item = session.get(NotificationOutbox, item_id)
                if item is None:
                    continue
                try:
                    payload = json.loads(item.payload)
                    url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
                    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
                    with urlopen(request, timeout=10) as response:
                        if response.status >= 300:
                            raise RuntimeError(f"Telegram HTTP {response.status}")
                except (HTTPError, URLError, OSError, ValueError, RuntimeError) as error:
                    item.attempts += 1
                    item.last_error = str(error)[:500]
                    item.status = "failed" if item.attempts >= 8 else "retry"
                    item.next_attempt_at = None if item.status == "failed" else self._utcnow() + timedelta(seconds=min(300, 2 ** item.attempts))
                    session.commit()
                    continue
                item.status = "sent"
                item.sent_at = self._utcnow()
                item.last_error = None
                session.commit()
                sent += 1
            finally:
                session.close()
        return sent

    def check_once(self) -> dict[str, Any]:
        statuses = {
            "database": self._database_status(),
            "internet": self._internet_status(),
            "disk": self._disk_status(),
            "erp": self._edge_status(),
            **self._camera_statuses(),
        }
        for component, (status, message) in statuses.items():
            self._update_incident(component, status, message)
        now = time.monotonic()
        if now - self.last_cleanup_at >= 86400:
            RecognitionEventStore(self.engine).purge_expired_photos()
            self.last_cleanup_at = now
        result = {
            "timestamp": self._utcnow().isoformat() + "Z",
            "overall_status": "ok" if all(status == "ok" for status, _ in statuses.values()) else "degraded",
            "components": {key: {"status": status, "message": message} for key, (status, message) in statuses.items()},
        }
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = HEALTH_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        temporary.replace(HEALTH_FILE)
        if self.edge_service and now - self.last_heartbeat_at >= self.settings.heartbeat_interval_seconds:
            runtime = read_runtime_status() or {}
            cameras = runtime.get("cameras") or ([runtime] if runtime.get("camera_id") else [])
            self.edge_service.queue_heartbeat({
                "edge_device_id": self.edge_settings.device_id,
                "timestamp": result["timestamp"],
                "overall_status": result["overall_status"],
                "internet_status": "online" if statuses["internet"][0] == "ok" else "offline",
                "erp_status": "available" if statuses["erp"][0] == "ok" else "unavailable",
                "db_status": "db_ok" if statuses["database"][0] == "ok" else "db_error",
                "application_status": "application_ok" if "application" not in statuses else "application_error",
                "cameras": [{
                    "camera_id": camera.get("camera_id"),
                    "status": "rtsp_connected" if camera.get("stream_status") == "connected" else "rtsp_disconnected",
                    "last_frame_age_ms": camera.get("frame_age_ms"),
                } for camera in cameras],
            })
            self.last_heartbeat_at = now
        self._deliver_notifications()
        return result


def health_worker(stop_event, log_level: str = "INFO") -> None:
    from core.logging_setup import configure_logging

    configure_logging(log_level)
    settings = load_health_settings()
    if not settings.enabled:
        return
    checker = HealthChecker(settings, load_edge_settings())
    while not stop_event.is_set():
        try:
            checker.check_once()
        except Exception:
            logger.exception("Health check failed")
        stop_event.wait(settings.check_interval_seconds)
