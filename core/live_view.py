"""Smooth MJPEG live view of the annotated camera frames (replay bench only).

Enabled only when VISION_OFFICE_LIVE_VIEW_PORT is set. The file-based preview
is capped at a few frames per second for production; this one serves every
annotated frame (up to ``max_fps``) over HTTP so a person can watch the
recognition in real time. Encoding runs in its own thread with a one-slot
mailbox, so a slow viewer only drops frames and never slows recognition.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2


logger = logging.getLogger("vision_office.live_view")
BOUNDARY = "frame"

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>LIVE {name}</title>
<style>html,body{{margin:0;height:100%;background:#000}}img{{width:100%;height:100%;object-fit:contain;display:block}}</style>
</head><body><img src="/stream" alt="live"></body></html>"""


def live_view_port_base() -> int | None:
    value = os.getenv("VISION_OFFICE_LIVE_VIEW_PORT", "").strip()
    return int(value) if value.isdigit() else None


class LiveView:
    def __init__(self, camera_id: str, camera_name: str, port_base: int, *, max_fps: float = 25.0,
                 max_width: int = 1920, jpeg_quality: int = 90, port_span: int = 10) -> None:
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.port_base = port_base
        self.port_span = port_span
        self.min_interval = 1.0 / max(1.0, float(max_fps))
        self.max_width = max_width
        self.jpeg_quality = jpeg_quality
        self.port: int | None = None
        self._condition = threading.Condition()
        self._raw = None
        self._jpeg: bytes | None = None
        self._jpeg_seq = 0
        self._last_submit = 0.0
        self._stopped = threading.Event()
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> "LiveView":
        view = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                if self.path.startswith("/stream"):
                    view._serve_stream(self)
                    return
                body = PAGE.format(name=view.camera_name).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        for port in range(self.port_base, self.port_base + self.port_span):
            try:
                self._server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            except OSError:
                continue
            self._server.daemon_threads = True
            self.port = port
            break
        if self._server is None:
            logger.warning("Live view: no free port in %s-%s", self.port_base, self.port_base + self.port_span - 1)
            return self
        threading.Thread(target=self._server.serve_forever, name=f"live-view-http-{self.camera_id}", daemon=True).start()
        threading.Thread(target=self._encode_loop, name=f"live-view-jpeg-{self.camera_id}", daemon=True).start()
        print(f"LIVE VIEW {self.camera_name}: port {self.port}", flush=True)
        return self

    def submit(self, frame) -> None:
        if self._server is None:
            return
        now = time.monotonic()
        if now - self._last_submit < self.min_interval:
            return
        self._last_submit = now
        with self._condition:
            self._raw = frame
            self._condition.notify_all()

    def _encode_loop(self) -> None:
        while not self._stopped.is_set():
            with self._condition:
                if self._raw is None:
                    self._condition.wait(timeout=0.5)
                frame, self._raw = self._raw, None
            if frame is None:
                continue
            height, width = frame.shape[:2]
            if width > self.max_width:
                frame = cv2.resize(frame, (self.max_width, max(1, round(height * self.max_width / width))),
                                   interpolation=cv2.INTER_AREA)
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
            if not ok:
                continue
            with self._condition:
                self._jpeg = encoded.tobytes()
                self._jpeg_seq += 1
                self._condition.notify_all()

    def _serve_stream(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        handler.send_header("Cache-Control", "no-cache, no-store")
        handler.end_headers()
        seen = -1
        try:
            while not self._stopped.is_set():
                with self._condition:
                    if self._jpeg_seq == seen:
                        self._condition.wait(timeout=1.0)
                    if self._jpeg is None or self._jpeg_seq == seen:
                        continue
                    data, seen = self._jpeg, self._jpeg_seq
                handler.wfile.write(
                    f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(data)}\r\n\r\n".encode("ascii"))
                handler.wfile.write(data)
                handler.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError, socket.error):
            return

    def stop(self) -> None:
        self._stopped.set()
        with self._condition:
            self._condition.notify_all()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
