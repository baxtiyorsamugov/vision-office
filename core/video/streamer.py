"""Low-latency camera reader with bounded reconnects for unstable RTSP feeds."""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlsplit

import cv2


logger = logging.getLogger("vision_office.video")


def safe_source_label(source) -> str:
    """Describe a stream in logs without ever exposing RTSP credentials."""
    value = str(source)
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        return value
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{parsed.path}"


class VideoStream:
    def __init__(self, src, reconnect_max_seconds: float = 10.0):
        self.src = src
        self.source_label = safe_source_label(src)
        self.is_file = not (str(src).lower().startswith(("rtsp://", "http://", "https://")) or str(src) == "0")
        self.reconnect_max_seconds = max(1.0, float(reconnect_max_seconds))
        self.lock = threading.Lock()
        self.stream = None
        self.frame = None
        self.frame_index = 0
        self.last_read_index = 0
        self.published_at = 0.0
        self.capture_fps = 0.0
        self._fps_window_started = time.monotonic()
        self._fps_window_frames = 0
        self.connected = False
        self.reconnect_attempts = 0
        self.last_error = None
        self.stopped = False
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.update, name=f"camera-reader-{self.source_label}", daemon=True)
        self.thread.start()
        return self

    def _open(self) -> bool:
        try:
            stream = cv2.VideoCapture(self.src)
            if not self.is_file:
                stream.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            ok, frame = stream.read()
            if not ok or frame is None:
                stream.release()
                raise RuntimeError("camera did not return a frame")
            with self.lock:
                previous = self.stream
                self.stream = stream
                self.connected = True
                self.last_error = None
                self.reconnect_attempts = 0
            if previous is not None:
                previous.release()
            self._publish(frame)
            logger.info("Camera connected source=%s", self.source_label)
            return True
        except Exception as error:
            with self.lock:
                self.connected = False
                self.last_error = str(error)
                self.reconnect_attempts += 1
            logger.warning("Camera connection failed source=%s error=%s", self.source_label, error)
            return False

    def _disconnect(self, reason: str) -> None:
        with self.lock:
            stream, self.stream = self.stream, None
            self.connected = False
            self.last_error = reason
            self.reconnect_attempts += 1
        if stream is not None:
            stream.release()
        logger.warning("Camera disconnected source=%s reason=%s", self.source_label, reason)

    def update(self):
        retry_delay = 0.5
        while not self.stopped:
            with self.lock:
                stream = self.stream
            if stream is None:
                if not self._open():
                    time.sleep(retry_delay)
                    retry_delay = min(self.reconnect_max_seconds, retry_delay * 2)
                    continue
                retry_delay = 0.5
                continue

            if self.is_file:
                ok, frame = stream.read()
                if not ok or frame is None:
                    stream.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                self._publish(frame)
                time.sleep(0.03)
                continue

            # grab/retrieve keeps only the newest RTSP frame and avoids latency buildup.
            if stream.grab():
                ok, frame = stream.retrieve()
                if ok and frame is not None:
                    self._publish(frame)
                    retry_delay = 0.5
                    continue
                self._disconnect("RTSP retrieve failed")
            else:
                self._disconnect("RTSP grab failed")
            time.sleep(retry_delay)
            retry_delay = min(self.reconnect_max_seconds, retry_delay * 2)

    def _publish(self, frame) -> None:
        with self.lock:
            self.frame = frame
            self.frame_index += 1
            self.published_at = time.monotonic()
            self._fps_window_frames += 1
            elapsed = self.published_at - self._fps_window_started
            if elapsed >= 1:
                self.capture_fps = self._fps_window_frames / elapsed
                self._fps_window_frames = 0
                self._fps_window_started = self.published_at

    def read(self):
        with self.lock:
            if self.frame_index == self.last_read_index:
                return None
            self.last_read_index = self.frame_index
            return self.frame.copy() if self.frame is not None else None

    def stats(self):
        with self.lock:
            return {
                "capture_fps": round(self.capture_fps, 1),
                "frame_age_ms": round(max(0.0, time.monotonic() - self.published_at) * 1000, 1) if self.published_at else None,
                "stream_status": "connected" if self.connected else "reconnecting",
                "reconnect_attempts": self.reconnect_attempts,
                "stream_error": self.last_error,
            }

    def stop(self):
        self.stopped = True
        with self.lock:
            stream, self.stream = self.stream, None
        if stream is not None:
            stream.release()
        if self.thread is not None:
            self.thread.join(timeout=2)
