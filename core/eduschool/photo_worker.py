"""Background source-photo enrollment, isolated from camera inference."""

from __future__ import annotations

import logging
import signal
import threading

from core.eduschool.auto_photos import EduSchoolAutoPhotoService
from core.eduschool.catalog import load_settings
from core.logging_setup import configure_logging


def main() -> None:
    configure_logging()
    logger = logging.getLogger("vision_office.eduschool.photos")
    settings = load_settings()
    if not settings.enabled:
        logger.info("EduSchool photo enrollment is disabled")
        return
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    service = EduSchoolAutoPhotoService(settings)
    recognizer = None
    while not stop.is_set():
        person_id = service.next_person_id()
        if person_id is None:
            stop.wait(10)
            continue
        if recognizer is None:
            from core.ai.recognizer import FaceRecognizer
            recognizer = FaceRecognizer(use_cuda=False)
        service.process_one(person_id, lambda image: recognizer.get_embedding(image, require_single=True))
        stop.wait(3)


if __name__ == "__main__":
    main()
