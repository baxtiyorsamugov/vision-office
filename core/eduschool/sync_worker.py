"""Hourly EduSchool directory refresh, independent from RTSP and event delivery."""

from __future__ import annotations

import logging
import signal
import threading

from core.eduschool.catalog import EduSchoolCatalogSync, load_settings
from core.logging_setup import configure_logging


def main() -> None:
    configure_logging()
    logger = logging.getLogger("vision_office.eduschool")
    settings = load_settings()
    if not settings.configured:
        logger.warning("EduSchool directory sync is not configured")
        return
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    service = EduSchoolCatalogSync(settings)
    while not stop.is_set():
        try:
            service.sync_once()
            delay = settings.sync_interval_seconds
        except Exception as error:
            logger.warning("EduSchool directory sync failed: %s", error)
            delay = min(60, settings.sync_interval_seconds)
        stop.wait(delay)


if __name__ == "__main__":
    main()
