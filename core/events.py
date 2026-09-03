"""Local recognition event audit log and short-lived event photo storage."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from sqlalchemy.orm import sessionmaker

from database.models import RecognitionEvent


logger = logging.getLogger("vision_office.events")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RecognitionEventStore:
    def __init__(self, engine, cooldown_seconds: int = 60, retention_days: int = 30):
        self.Session = sessionmaker(bind=engine)
        self.cooldown_seconds = max(0, cooldown_seconds)
        self.retention_days = max(1, retention_days)
        RecognitionEvent.__table__.create(engine, checkfirst=True)

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.utcnow()

    @staticmethod
    def _signature(identity: dict[str, str] | None, embedding: np.ndarray | None, hint: str | None) -> str:
        if identity and identity.get("person_id"):
            return str(identity["person_id"])
        if hint:
            return hint[:128]
        if embedding is None:
            return "unknown"
        raw = np.asarray(embedding, dtype=np.float32).tobytes()
        return hashlib.sha256(raw).hexdigest()

    def _save_photo(self, event_id: str, image: np.ndarray | None) -> str | None:
        if image is None or image.size == 0:
            return None
        day = datetime.utcnow().strftime("%Y-%m-%d")
        folder = PROJECT_ROOT / "data" / "events" / day
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{event_id}.jpg"
        if not cv2.imwrite(str(destination), image):
            logger.warning("Could not write event image event_id=%s", event_id)
            return None
        return str(destination.relative_to(PROJECT_ROOT)).replace("\\", "/")

    def record(
        self,
        *,
        camera_id: str,
        event_type: str,
        identity: dict[str, str] | None,
        confidence: float | None,
        embedding: np.ndarray | None,
        image: np.ndarray | None,
        subject_hint: str | None = None,
    ) -> RecognitionEvent | None:
        """Write one event unless this subject recently passed the same camera."""
        signature = self._signature(identity, embedding, subject_hint)
        now = self._utcnow()
        session = self.Session()
        try:
            recent = session.query(RecognitionEvent).filter(
                RecognitionEvent.camera_id == camera_id,
                RecognitionEvent.event_type == event_type,
                RecognitionEvent.subject_signature == signature,
                RecognitionEvent.created_at >= now - timedelta(seconds=self.cooldown_seconds),
            ).first()
            if recent:
                return None
            event_id = str(uuid.uuid4())
            event = RecognitionEvent(
                id=event_id,
                camera_id=camera_id,
                event_type=event_type,
                person_id=identity.get("person_id") if identity else None,
                person_type=identity.get("person_type", "employee") if identity else "unknown",
                person_name=identity.get("name") if identity else None,
                subject_signature=signature,
                confidence=round(float(confidence), 6) if confidence is not None else None,
                photo_path=self._save_photo(event_id, image),
                created_at=now,
            )
            session.add(event)
            session.commit()
            session.refresh(event)
            logger.info(
                "Recognition event recorded event_id=%s camera_id=%s type=%s person_id=%s",
                event.id, camera_id, event_type, event.person_id,
            )
            return event
        except Exception:
            session.rollback()
            logger.exception("Could not record recognition event camera_id=%s", camera_id)
            return None
        finally:
            session.close()

    def purge_expired_photos(self) -> int:
        """Delete only retained image files; preserve event metadata for audit."""
        cutoff = self._utcnow() - timedelta(days=self.retention_days)
        session = self.Session()
        removed = 0
        try:
            events = session.query(RecognitionEvent).filter(
                RecognitionEvent.created_at < cutoff,
                RecognitionEvent.photo_path.is_not(None),
            ).all()
            for event in events:
                path = PROJECT_ROOT / event.photo_path
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    logger.warning("Could not delete expired event image path=%s error=%s", path, error)
                    continue
                event.photo_path = None
                removed += 1
            session.commit()
            return removed
        finally:
            session.close()
