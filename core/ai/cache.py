"""Refresh immutable face vectors away from the inference queue."""
import logging
import threading
import time


logger = logging.getLogger("vision_office.face_cache")


class FaceCache:
    def __init__(self, loader, interval=5):
        self.loader = loader
        self.interval = interval
        self.snapshot = loader()
        self.last_success = time.monotonic()
        self.last_refresh_ms = None
        self.error = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="face-cache", daemon=True)

    def refresh(self):
        started = time.perf_counter()
        try:
            snapshot = self.loader()
            self.snapshot = snapshot
            self.last_success = time.monotonic()
            self.error = None
        except Exception as error:
            self.error = type(error).__name__
            logger.warning("Face cache refresh failed: %s", self.error)
        self.last_refresh_ms = round((time.perf_counter() - started) * 1000, 1)

    @property
    def stale(self):
        return time.monotonic() - self.last_success > max(30, self.interval * 3)

    def _run(self):
        while not self.stop_event.wait(self.interval):
            self.refresh()

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=1)
