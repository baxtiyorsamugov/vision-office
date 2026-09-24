"""Isolated opt-in sender for EduSchool employee attendance."""

from __future__ import annotations

import logging
import signal
import threading

from core.eduschool.turnstile import EduSchoolTurnstileService, load_settings
from core.logging_setup import configure_logging


def main() -> None:
    configure_logging()
    logger = logging.getLogger("vision_office.eduschool.turnstile")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    settings = load_settings()
    service = EduSchoolTurnstileService(settings)
    active = service.prepare_activation()
    if not active:
        logger.warning("EduSchool attendance delivery is disabled: %s", settings.validation_error() or "operator setting")
    while not stop.is_set():
        if active:
            try:
                qualified = service.refresh_auto_approvals()
                queued = service.queue_new_events()
                sent = service.deliver_due()
                if qualified or queued or sent:
                    logger.info("EduSchool attendance qualified=%d scan=%d sent=%d", qualified, queued, sent)
            except Exception:
                logger.exception("EduSchool attendance worker cycle failed")
        stop.wait(settings.poll_interval_seconds)


if __name__ == "__main__":
    main()
