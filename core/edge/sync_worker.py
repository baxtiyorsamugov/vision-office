"""Dedicated ERP synchronization and outbox delivery process."""

from __future__ import annotations

import logging
import multiprocessing as mp
import signal

from core.edge.config import load_edge_settings
from core.edge.service import EdgeService
from core.logging_setup import configure_logging


logger = logging.getLogger("vision_office.edge_sync")


def run_sync_loop(stop_event) -> None:
    """Run one shared sync loop; hourly people sync is enforced by EdgeService."""
    settings = load_edge_settings()
    if not settings.configured:
        logger.warning("ERP sync is idle because edge_integration is not configured")
        while not stop_event.wait(30):
            pass
        return

    service = EdgeService(settings)
    logger.info(
        "ERP sync worker started people_interval_seconds=%s delivery_poll_seconds=%s",
        settings.sync_interval_seconds,
        settings.delivery_poll_interval_seconds,
    )
    while not stop_event.is_set():
        try:
            service.maintenance()
        except Exception:
            logger.exception("ERP sync maintenance failed")
        stop_event.wait(settings.delivery_poll_interval_seconds)


def main() -> None:
    configure_logging()
    stop_event = mp.Event()

    def _stop(_signum, _frame) -> None:
        stop_event.set()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _stop)
    run_sync_loop(stop_event)


if __name__ == "__main__":
    main()
