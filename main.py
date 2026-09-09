"""Vision Office live camera entrypoint and multi-camera supervisor target."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import signal
import time

import cv2
import onnxruntime as ort

from core.ai.engine import AI_Engine
from core.config import AppSettings, CameraSettings, ConfigurationError, load_app_settings
from core.edge.config import load_edge_settings
from core.hr import HRManager
from core.logging_setup import configure_logging
from core.overlay import draw_detection_label
from core.performance import write_camera_runtime_status
from core.preview import CameraPreviewPublisher
from core.video.streamer import VideoStream
from database.manager import init_db


logger = logging.getLogger("vision_office.main")


def headless_mode() -> bool:
    """Return whether this runtime has no desktop available for OpenCV windows."""
    return os.getenv("VISION_OFFICE_HEADLESS", "").strip().lower() in {"1", "true", "yes", "on"}


def run_camera(camera: CameraSettings, ai_settings: dict, log_level: str = "INFO") -> None:
    """Run one camera in its own process. A failure never affects sibling cameras."""
    configure_logging(log_level)
    init_db()
    logger.info("Starting camera camera_id=%s event_type=%s", camera.id, camera.event_type)
    write_camera_runtime_status(camera.id, {"running": False, "ai_ready": False})
    ai = AI_Engine(
        detection_imgsz=ai_settings.get("face_detection_imgsz", 960),
        detection_fps=ai_settings.get("face_detection_fps", 20),
        camera_id=camera.id,
        event_type=camera.event_type,
    )
    # HRManager filters on the local identity prefix, so ERP recognition is
    # never written to the device-only attendance table.
    hr = HRManager(cooldown_minutes=1)
    stream = VideoStream(camera.rtsp_url).start()
    preview = CameraPreviewPublisher(
        camera.id,
        max_fps=ai_settings.get("monitor_preview_fps", 4),
        max_width=ai_settings.get("monitor_preview_width", 960),
        jpeg_quality=ai_settings.get("monitor_preview_jpeg_quality", 70),
    ).start()
    window_name = f"Smart Vision AI - {camera.name}"
    headless = headless_mode()
    last_status_update = 0.0
    try:
        while True:
            if not ai.worker.is_alive() or not ai.detector_thread.is_alive():
                raise RuntimeError("AI worker stopped; camera process needs restart")
            if time.monotonic() - last_status_update >= 1:
                # Publish even without a frame. This lets Health Checker detect a frozen reader.
                write_camera_runtime_status(camera.id, {
                    "running": True,
                    "camera_name": camera.name,
                    "event_type": camera.event_type,
                    "location": camera.location,
                    "onnx_providers": ort.get_available_providers(),
                    **stream.stats(),
                    **preview.stats(),
                    **ai.status_snapshot(),
                })
                last_status_update = time.monotonic()
            frame = stream.read()
            if frame is None:
                time.sleep(0.005)
                continue
            results = ai.process_frame(frame)

            for item in results:
                x1, y1, x2, y2 = item["box"]
                name = item["name"]
                hr.register_presence(item, event_type=camera.event_type)
                frame = draw_detection_label(frame, (x1, y1, x2, y2), name)

            # Preview encoding runs in a separate one-slot mailbox. It never
            # starts another RTSP reader and drops frames instead of slowing AI.
            preview.submit(frame)

            if not headless:
                preview = cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_AREA)
                cv2.imshow(window_name, preview)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except Exception:
        logger.exception("Camera worker failed camera_id=%s", camera.id)
        raise
    finally:
        write_camera_runtime_status(camera.id, {
            "running": False,
            "camera_name": camera.name,
            **stream.stats(),
            **preview.stats(),
        })
        stream.stop()
        preview.stop()
        ai.stop()
        if not headless:
            cv2.destroyWindow(window_name)


def _stop_on_signal(_signum, _frame) -> None:
    """Turn Docker's SIGTERM into normal cleanup for the supervisor/camera loop."""
    raise KeyboardInterrupt


def main() -> None:
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop_on_signal)
    try:
        settings: AppSettings = load_app_settings()
    except ConfigurationError as error:
        raise SystemExit(f"Configuration error: {error}") from error
    configure_logging(settings.log_level)
    edge_settings = load_edge_settings()
    if error := edge_settings.validation_error():
        raise SystemExit(f"Configuration error: {error}")
    init_db()
    active_cameras = tuple(camera for camera in settings.cameras if camera.is_active)
    if len(active_cameras) == 1:
        from core.health import health_worker

        health_stop_event = mp.Event()
        health_process = mp.Process(
            target=health_worker,
            args=(health_stop_event, settings.log_level),
            name="vision-health-checker",
            daemon=True,
        )
        health_process.start()
        try:
            run_camera(active_cameras[0], settings.ai, settings.log_level)
        finally:
            health_stop_event.set()
            health_process.join(timeout=5)
            if health_process.is_alive():
                health_process.terminate()
        return
    from core.supervisor import CameraSupervisor

    logger.info("Starting multi-camera supervisor camera_count=%s", len(active_cameras))
    CameraSupervisor(active_cameras, settings.ai, settings.log_level).run()


if __name__ == "__main__":
    main()
