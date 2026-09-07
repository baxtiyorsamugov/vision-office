"""Bounded, local camera-preview publishing that never opens an RTSP stream."""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from pathlib import Path

import cv2

from core.performance import PROJECT_ROOT


logger = logging.getLogger("vision_office.preview")
PREVIEW_DIRECTORY = PROJECT_ROOT / "data" / "previews"


def preview_path(camera_id: str, directory: Path = PREVIEW_DIRECTORY) -> Path:
    """Return a safe local preview filename for a configured camera id."""
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(camera_id))[:100] or "camera"
    return directory / f"{safe_id}.jpg"


class CameraPreviewPublisher:
    """Encode a best-effort preview outside the recognition and RTSP hot paths.

    The one-slot queue deliberately discards stale frames. A slow disk or JPEG
    encoder can therefore never queue work, block camera capture or delay AI.
    """

    def __init__(
        self,
        camera_id: str,
        max_fps: float = 4.0,
        max_width: int = 960,
        jpeg_quality: int = 70,
        output_directory: Path = PREVIEW_DIRECTORY,
    ) -> None:
        self.camera_id = camera_id
        self.target = preview_path(camera_id, output_directory)
        self.max_fps = min(8.0, max(0.5, float(max_fps)))
        self.max_width = min(1920, max(320, int(max_width)))
        self.jpeg_quality = min(95, max(40, int(jpeg_quality)))
        self._frames: queue.Queue = queue.Queue(maxsize=1)
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._last_submitted_at = 0.0
        self._updated_at = 0.0
        self._dropped_frames = 0
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "CameraPreviewPublisher":
        self._thread = threading.Thread(
            target=self._run,
            name=f"camera-preview-{self.camera_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def submit(self, frame) -> None:
        """Offer a final annotated frame without waiting for JPEG encoding."""
        if self._stopped.is_set():
            return
        now = time.monotonic()
        with self._lock:
            if now - self._last_submitted_at < 1.0 / self.max_fps:
                return
            self._last_submitted_at = now
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frames.put_nowait(frame)
            except queue.Full:
                pass
            with self._lock:
                self._dropped_frames += 1

    def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                frame = self._frames.get(timeout=0.25)
            except queue.Empty:
                continue
            if frame is None:
                return
            try:
                self._write(frame)
            except Exception as error:
                with self._lock:
                    self._last_error = str(error)
                logger.warning("Camera preview write failed camera_id=%s error=%s", self.camera_id, error)

    def _write(self, frame) -> None:
        height, width = frame.shape[:2]
        if width > self.max_width:
            target_height = max(1, round(height * self.max_width / width))
            frame = cv2.resize(frame, (self.max_width, target_height), interpolation=cv2.INTER_AREA)
        encoded_ok, encoded = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        )
        if not encoded_ok:
            raise RuntimeError("JPEG encoding failed")
        self.target.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.target.with_name(
            f".{self.target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_bytes(encoded.tobytes())
            os.replace(temporary, self.target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        with self._lock:
            self._updated_at = time.time()
            self._last_error = None

    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "preview_available": self.target.is_file(),
                "preview_updated_at": self._updated_at or None,
                "preview_dropped_frames": self._dropped_frames,
                "preview_error": self._last_error,
            }

    def stop(self) -> None:
        self._stopped.set()
        try:
            self._frames.put_nowait(None)
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self._frames.put_nowait(None)
            except queue.Full:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
