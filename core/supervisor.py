"""Independent camera worker supervision for entry/exit deployments."""

from __future__ import annotations

import logging
import multiprocessing as mp
import time
from dataclasses import asdict

from core.config import CameraSettings


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

    def _start_camera(self, camera: CameraSettings) -> None:
        process = mp.Process(
            target=camera_process,
            args=(asdict(camera), self.ai_settings, self.log_level),
            name=f"vision-camera-{camera.id}",
        )
        process.start()
        self.processes[camera.id] = process
        logger.info("Started camera process camera_id=%s pid=%s", camera.id, process.pid)

    def run(self) -> None:
        from core.health import health_worker

        self.health_process = mp.Process(
            target=health_worker,
            args=(self.health_stop_event, self.log_level),
            name="vision-health-checker",
            daemon=True,
        )
        self.health_process.start()
        for camera in self.cameras:
            self._start_camera(camera)
        try:
            while not self.stopping:
                time.sleep(1)
                for camera in self.cameras:
                    process = self.processes.get(camera.id)
                    if process is None or process.is_alive() or self.stopping:
                        continue
                    self.restart_count[camera.id] += 1
                    delay = min(30, 2 ** min(self.restart_count[camera.id], 5))
                    logger.error("Camera process exited camera_id=%s code=%s restart_in=%ss", camera.id, process.exitcode, delay)
                    time.sleep(delay)
                    self._start_camera(camera)
        except KeyboardInterrupt:
            logger.info("Supervisor shutdown requested")
        finally:
            self.stop()

    def stop(self) -> None:
        self.stopping = True
        self.health_stop_event.set()
        for process in self.processes.values():
            if process.is_alive():
                process.terminate()
        for process in self.processes.values():
            process.join(timeout=5)
        if self.health_process is not None:
            self.health_process.join(timeout=5)
            if self.health_process.is_alive():
                self.health_process.terminate()
