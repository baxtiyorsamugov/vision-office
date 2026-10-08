"""Independent camera worker supervision for entry/exit deployments."""

from __future__ import annotations

import logging
import multiprocessing as mp
import time
from dataclasses import asdict

from core.config import CameraSettings, load_app_settings
from core.camera_controls import camera_ai_settings
from core.performance import PROJECT_ROOT, _write_status_file, write_camera_runtime_status


logger = logging.getLogger("vision_office.supervisor")


def camera_process(camera_data: dict, ai_settings: dict, log_level: str) -> None:
    """Child entrypoint kept at module scope for Windows multiprocessing spawn."""
    from main import run_camera

    run_camera(CameraSettings(**camera_data), ai_settings, log_level)


class CameraSupervisor:
    def __init__(self, cameras: tuple[CameraSettings, ...], ai_settings: dict, log_level: str):
        self.cameras = cameras
        self.ai_settings = ai_settings
        self.log_level = log_level
        self.processes: dict[str, mp.Process] = {}
        self.restart_count: dict[str, int] = {camera.id: 0 for camera in cameras}
        self.stopping = False
        self.health_stop_event = mp.Event()
        self.health_process = None
        self.context = mp.get_context("spawn")
        self.restart_at = {}
        self.started_at = {}

    def _start_camera(self, camera: CameraSettings) -> None:
        process = self.context.Process(
            target=camera_process,
            args=(asdict(camera), camera_ai_settings(camera, self.ai_settings), self.log_level),
            name=f"vision-camera-{camera.id}",
        )
        process.start()
        self.processes[camera.id] = process
        self.started_at[camera.id] = time.monotonic()
        logger.info("Started camera process camera_id=%s pid=%s", camera.id, process.pid)

    def _stop_camera(self, camera_id):
        process = self.processes.pop(camera_id, None)
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=12)
            if process.is_alive():
                process.kill()
                process.join(timeout=3)
        self.restart_at.pop(camera_id, None)
        write_camera_runtime_status(camera_id, {"running": False, "ai_ready": False, "stream_status": "stopped"})

    def reconcile(self, cameras, ai_settings):
        previous = {camera.id: camera for camera in self.cameras}
        desired = {camera.id: camera for camera in cameras if camera.is_active}
        for camera_id in list(self.processes):
            if camera_id not in desired or previous.get(camera_id) != desired[camera_id] or ai_settings != self.ai_settings:
                self._stop_camera(camera_id)
        self.cameras = cameras
        self.ai_settings = ai_settings
        now = time.monotonic()
        for camera_id, camera in desired.items():
            process = self.processes.get(camera_id)
            if process is None:
                self.restart_count[camera_id] = 0
                self._start_camera(camera)
            elif not process.is_alive():
                if camera_id not in self.restart_at:
                    failures = self.restart_count.get(camera_id, 0) + 1
                    self.restart_count[camera_id] = failures
                    self.restart_at[camera_id] = now + min(30, 2 ** min(failures, 5))
                    logger.error("Camera process exited camera_id=%s code=%s", camera_id, process.exitcode)
                elif now >= self.restart_at[camera_id]:
                    process.join(timeout=0)
                    self.restart_at.pop(camera_id)
                    self._start_camera(camera)
            elif now - self.started_at.get(camera_id, now) > 120:
                self.restart_count[camera_id] = 0

    def run(self) -> None:
        from core.health import health_worker
        from core.resources import sample_resources

        self.health_process = mp.Process(
            target=health_worker,
            args=(self.health_stop_event, self.log_level),
            name="vision-health-checker",
            daemon=True,
        )
        self.health_process.start()
        try:
            while not self.stopping:
                error = None
                try:
                    settings = load_app_settings()
                    self.reconcile(settings.cameras, settings.ai)
                except (OSError, ValueError) as problem:
                    error = "Invalid camera configuration; previous settings retained"
                    logger.error("%s (%s)", error, type(problem).__name__)
                try:
                    resources = sample_resources()
                except (OSError, ValueError):
                    resources = {}
                _write_status_file(PROJECT_ROOT / "data" / "supervisor_status.json", {
                    "running": True, "updated_at": time.time(), "error": error,
                    "active_cameras": [camera.id for camera in self.cameras if camera.is_active],
                    "resources": resources,
                })
                time.sleep(2)
        except KeyboardInterrupt:
            logger.info("Supervisor shutdown requested")
        finally:
            self.stop()

    def stop(self) -> None:
        self.stopping = True
        self.health_stop_event.set()
        for camera_id in list(self.processes):
            self._stop_camera(camera_id)
        if self.health_process is not None:
            self.health_process.join(timeout=5)
            if self.health_process.is_alive():
                self.health_process.terminate()
        _write_status_file(PROJECT_ROOT / "data" / "supervisor_status.json", {"running": False, "updated_at": time.time()})
