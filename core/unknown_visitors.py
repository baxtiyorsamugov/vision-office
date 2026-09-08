"""Device-local clustering and review workflow for unknown face observations."""

from __future__ import annotations

import json
import logging
import shutil
import signal
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
import yaml
from sqlalchemy.orm import sessionmaker

from database.manager import get_engine
from database.migrations import run_migrations
from database.models import Employee, RecognitionEvent, UnknownFaceObservation, UnknownVisitor, UnknownVisitorVisit


logger = logging.getLogger("vision_office.unknown_visitors")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATUS_FILE = PROJECT_ROOT / "data" / "unknown_clusterer_status.json"


@dataclass(frozen=True)
class UnknownVisitorSettings:
    enabled: bool = True
    similarity_threshold: float = 0.55
    visit_gap_seconds: int = 600
    retention_days: int = 30
    poll_interval_seconds: int = 5
    backfill_batch_size: int = 1


def load_unknown_visitor_settings(path: str | Path = "config/settings.yaml") -> UnknownVisitorSettings:
    config_path = Path(path)
    if not config_path.is_file():
        return UnknownVisitorSettings()
    with config_path.open("r", encoding="utf-8") as stream:
        values = (yaml.safe_load(stream) or {}).get("unknown_visitors") or {}
    defaults = UnknownVisitorSettings()
    return UnknownVisitorSettings(
        enabled=bool(values.get("enabled", defaults.enabled)),
        similarity_threshold=min(0.95, max(0.05, float(values.get("similarity_threshold", defaults.similarity_threshold)))),
        visit_gap_seconds=min(86400, max(30, int(values.get("visit_gap_seconds", defaults.visit_gap_seconds)))),
        retention_days=min(365, max(1, int(values.get("retention_days", defaults.retention_days)))),
        poll_interval_seconds=min(300, max(1, int(values.get("poll_interval_seconds", defaults.poll_interval_seconds)))),
        backfill_batch_size=min(25, max(1, int(values.get("backfill_batch_size", defaults.backfill_batch_size)))),
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_embedding(value: Iterable[float] | np.ndarray | None) -> list[float] | None:
    if value is None:
        return None
    vector = np.asarray(value, dtype=np.float32)
    if vector.shape != (512,) or not np.isfinite(vector).all():
        return None
    norm = float(np.linalg.norm(vector))
    if norm == 0:
        return None
    return (vector / norm).tolist()


def add_unknown_observation(session, event: RecognitionEvent, embedding, enabled: bool = True) -> UnknownFaceObservation | None:
    """Append a pending sample without doing clustering work in the camera process."""
    if not enabled or event.person_type != "unknown":
        return None
    normalized = normalize_embedding(embedding)
    if normalized is None:
        return None
    observation = UnknownFaceObservation(
        id=str(uuid.uuid4()),
        event_id=event.id,
        embedding=normalized,
        processing_status="pending",
        observed_at=event.created_at,
    )
    session.add(observation)
    return observation


class UnknownVisitorService:
    """Keeps unknown-face grouping out of the latency-sensitive camera worker."""

    def __init__(self, engine=None, settings: UnknownVisitorSettings | None = None):
        self.engine = engine or get_engine()
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.settings = settings or load_unknown_visitor_settings()
        self._recognizer = None

    def record_observation(self, session, event: RecognitionEvent, embedding) -> UnknownFaceObservation | None:
        """Add a pending sample in the event transaction; never cluster from the camera process."""
        return add_unknown_observation(session, event, embedding, self.settings.enabled)

    @staticmethod
    def _embedding(value) -> np.ndarray | None:
        normalized = normalize_embedding(value)
        return np.asarray(normalized, dtype=np.float32) if normalized is not None else None

    def _active_candidates(self, session) -> list[tuple[UnknownVisitor, np.ndarray]]:
        candidates = []
        for visitor in session.query(UnknownVisitor).filter(UnknownVisitor.state == "active").all():
            vector = self._embedding(visitor.canonical_embedding)
            if vector is not None:
                candidates.append((visitor, vector))
        return candidates

    def _choose_visitor(self, session, embedding: np.ndarray, observed_at: datetime) -> UnknownVisitor:
        best_visitor = None
        best_score = -1.0
        for visitor, candidate in self._active_candidates(session):
            score = float(np.dot(candidate, embedding))
            if score > best_score:
                best_visitor, best_score = visitor, score
        if best_visitor is not None and best_score >= self.settings.similarity_threshold:
            return best_visitor
        visitor = UnknownVisitor(
            state="active",
            canonical_embedding=embedding.tolist(),
            first_seen_at=observed_at,
            last_seen_at=observed_at,
        )
        session.add(visitor)
        session.flush()
        return visitor

    def _observations_for_visitor(self, session, visitor_id: int) -> list[tuple[UnknownFaceObservation, RecognitionEvent]]:
        return session.query(UnknownFaceObservation, RecognitionEvent).join(
            RecognitionEvent, RecognitionEvent.id == UnknownFaceObservation.event_id
        ).filter(
            UnknownFaceObservation.visitor_id == visitor_id,
            UnknownFaceObservation.processing_status == "clustered",
            UnknownFaceObservation.embedding.is_not(None),
        ).order_by(UnknownFaceObservation.observed_at.asc()).all()

    def _rebuild_visitor(self, session, visitor: UnknownVisitor) -> None:
        rows = self._observations_for_visitor(session, visitor.id)
        session.query(UnknownVisitorVisit).filter(UnknownVisitorVisit.visitor_id == visitor.id).delete()
        if not rows:
            visitor.canonical_embedding = None
            visitor.observation_count = 0
            visitor.visit_count = 0
            visitor.primary_photo_path = None
            if visitor.state == "active":
                visitor.state = "archived"
            visitor.updated_at = utc_now()
            return

        vectors = [self._embedding(observation.embedding) for observation, _event in rows]
        centroid = normalize_embedding(np.mean(np.vstack(vectors), axis=0))
        visitor.canonical_embedding = centroid
        visitor.observation_count = len(rows)
        visitor.first_seen_at = rows[0][0].observed_at
        visitor.last_seen_at = rows[-1][0].observed_at
        visitor.primary_photo_path = next((event.photo_path for _observation, event in reversed(rows) if event.photo_path), None)
        gap = timedelta(seconds=self.settings.visit_gap_seconds)
        active_visits: dict[tuple[str, str], UnknownVisitorVisit] = {}
        visits_created = 0
        for observation, event in rows:
            key = (event.camera_id, event.event_type)
            visit = active_visits.get(key)
            if visit is None or observation.observed_at - visit.last_seen_at > gap:
                visit = UnknownVisitorVisit(
                    id=str(uuid.uuid4()),
                    visitor_id=visitor.id,
                    camera_id=event.camera_id,
                    event_type=event.event_type,
                    started_at=observation.observed_at,
                    last_seen_at=observation.observed_at,
                    observation_count=1,
                )
                session.add(visit)
                active_visits[key] = visit
                visits_created += 1
            else:
                visit.last_seen_at = observation.observed_at
                visit.observation_count += 1
        visitor.visit_count = visits_created
        visitor.updated_at = utc_now()

    def _cluster_observation(self, session, observation: UnknownFaceObservation) -> UnknownVisitor | None:
        embedding = self._embedding(observation.embedding)
        event = session.get(RecognitionEvent, observation.event_id)
        if embedding is None or event is None:
            observation.processing_status = "failed"
            observation.processing_error = "Missing event or invalid face embedding"
            return None
        visitor = self._choose_visitor(session, embedding, observation.observed_at)
        observation.visitor_id = visitor.id
        observation.processing_status = "clustered"
        observation.processing_error = None
        self._rebuild_visitor(session, visitor)
        return visitor

    def process_pending(self, limit: int = 25) -> int:
        if not self.settings.enabled:
            return 0
        session = self.Session()
        processed = 0
        try:
            observations = session.query(UnknownFaceObservation).filter(
                UnknownFaceObservation.processing_status == "pending"
            ).order_by(UnknownFaceObservation.observed_at.asc()).limit(limit).all()
            for observation in observations:
                self._cluster_observation(session, observation)
                processed += 1
            session.commit()
            return processed
        except Exception:
            session.rollback()
            logger.exception("Unknown visitor clustering cycle failed")
            return 0
        finally:
            session.close()

    def _event_photo_path(self, event: RecognitionEvent) -> Path | None:
        if not event.photo_path:
            return None
        try:
            path = (PROJECT_ROOT / event.photo_path).resolve()
            path.relative_to((PROJECT_ROOT / "data" / "events").resolve())
        except (OSError, ValueError):
            return None
        return path if path.is_file() else None

    def _get_recognizer(self):
        if self._recognizer is None:
            from core.ai.recognizer import FaceRecognizer

            self._recognizer = FaceRecognizer()
        return self._recognizer

    def backfill_missing(self, limit: int | None = None) -> int:
        """Derive pending observations from retained event photos at a controlled rate."""
        if not self.settings.enabled:
            return 0
        session = self.Session()
        added = 0
        try:
            events = session.query(RecognitionEvent).outerjoin(
                UnknownFaceObservation, UnknownFaceObservation.event_id == RecognitionEvent.id
            ).filter(
                RecognitionEvent.person_type == "unknown",
                UnknownFaceObservation.id.is_(None),
            ).order_by(RecognitionEvent.created_at.asc()).limit(limit or self.settings.backfill_batch_size).all()
            for event in events:
                path = self._event_photo_path(event)
                image = cv2.imread(str(path)) if path else None
                embedding = self._get_recognizer().get_embedding(image) if image is not None else None
                normalized = normalize_embedding(embedding)
                observation = UnknownFaceObservation(
                    id=str(uuid.uuid4()),
                    event_id=event.id,
                    embedding=normalized,
                    processing_status="pending" if normalized is not None else "failed",
                    processing_error=None if normalized is not None else "Photo is missing, invalid, or contains no face",
                    observed_at=event.created_at,
                )
                session.add(observation)
                added += 1
            session.commit()
            return added
        except Exception:
            session.rollback()
            logger.exception("Unknown visitor history backfill failed")
            return 0
        finally:
            session.close()

    def purge_expired(self) -> int:
        cutoff = utc_now() - timedelta(days=self.settings.retention_days)
        session = self.Session()
        changed = 0
        try:
            observations = session.query(UnknownFaceObservation).filter(
                UnknownFaceObservation.observed_at < cutoff,
                UnknownFaceObservation.embedding.is_not(None),
            ).all()
            visitor_ids = {observation.visitor_id for observation in observations if observation.visitor_id is not None}
            for observation in observations:
                observation.embedding = None
                observation.processing_status = "expired"
                observation.processing_error = "Retention period expired"
                changed += 1
            for visitor_id in visitor_ids:
                visitor = session.get(UnknownVisitor, visitor_id)
                if visitor is not None and visitor.state != "converted":
                    self._rebuild_visitor(session, visitor)
            session.commit()
            return changed
        except Exception:
            session.rollback()
            logger.exception("Unknown visitor retention cleanup failed")
            return 0
        finally:
            session.close()

    def merge(self, source_id: int, target_id: int) -> UnknownVisitor:
        if source_id == target_id:
            raise ValueError("Выберите две разные карточки.")
        session = self.Session()
        try:
            source, target = session.get(UnknownVisitor, source_id), session.get(UnknownVisitor, target_id)
            if source is None or target is None or source.state != "active" or target.state != "active":
                raise ValueError("Объединять можно только активные карточки неизвестных.")
            session.query(UnknownFaceObservation).filter(UnknownFaceObservation.visitor_id == source.id).update({"visitor_id": target.id})
            source.state = "archived"
            source.merged_into_id = target.id
            self._rebuild_visitor(session, source)
            self._rebuild_visitor(session, target)
            session.commit()
            return target
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def split(self, visitor_id: int, observation_ids: list[str]) -> UnknownVisitor:
        if not observation_ids:
            raise ValueError("Выберите хотя бы одно наблюдение для новой карточки.")
        session = self.Session()
        try:
            source = session.get(UnknownVisitor, visitor_id)
            if source is None or source.state != "active":
                raise ValueError("Карточка неизвестного больше не активна.")
            observations = session.query(UnknownFaceObservation).filter(
                UnknownFaceObservation.visitor_id == visitor_id,
                UnknownFaceObservation.id.in_(observation_ids),
                UnknownFaceObservation.processing_status == "clustered",
            ).all()
            if len(observations) != len(set(observation_ids)):
                raise ValueError("Часть выбранных наблюдений недоступна для разделения.")
            target = UnknownVisitor(state="active", first_seen_at=min(item.observed_at for item in observations), last_seen_at=max(item.observed_at for item in observations))
            session.add(target)
            session.flush()
            for observation in observations:
                observation.visitor_id = target.id
            self._rebuild_visitor(session, source)
            self._rebuild_visitor(session, target)
            session.commit()
            return target
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def convert_to_local_employee(self, visitor_id: int, full_name: str, role: str, primary_observation_id: str, extra_observation_ids: list[str] | None = None) -> Employee:
        if not full_name.strip() or not role.strip():
            raise ValueError("Укажите полное имя и роль локального сотрудника.")
        session = self.Session()
        destination = None
        try:
            visitor = session.get(UnknownVisitor, visitor_id)
            if visitor is None or visitor.state != "active":
                raise ValueError("Карточка неизвестного недоступна для регистрации.")
            observation_ids = [primary_observation_id] + [item for item in (extra_observation_ids or []) if item != primary_observation_id]
            observation_ids = observation_ids[:5]
            rows = session.query(UnknownFaceObservation, RecognitionEvent).join(
                RecognitionEvent, RecognitionEvent.id == UnknownFaceObservation.event_id
            ).filter(
                UnknownFaceObservation.visitor_id == visitor_id,
                UnknownFaceObservation.id.in_(observation_ids),
                UnknownFaceObservation.processing_status == "clustered",
            ).all()
            by_id = {observation.id: (observation, event) for observation, event in rows}
            if primary_observation_id not in by_id:
                raise ValueError("Выберите действительное основное наблюдение.")
            primary_observation, primary_event = by_id[primary_observation_id]
            source_path = self._event_photo_path(primary_event)
            if source_path is None:
                raise ValueError("Исходная фотография уже недоступна. Выберите другое наблюдение.")
            vectors = [normalize_embedding(by_id[item][0].embedding) for item in observation_ids if item in by_id]
            vectors = [item for item in vectors if item is not None]
            if not vectors:
                raise ValueError("Не найдено действительных биометрических шаблонов.")
            faces_dir = PROJECT_ROOT / "data" / "faces"
            faces_dir.mkdir(parents=True, exist_ok=True)
            safe_name = "_".join("".join(char for char in full_name.strip() if char.isalnum() or char in " _-").split()) or "local"
            destination = faces_dir / f"{safe_name}_{time.time_ns()}.jpg"
            shutil.copy2(source_path, destination)
            employee = Employee(
                full_name=full_name.strip(),
                role=role.strip(),
                face_embeddings=vectors,
                photo_path=str(destination.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            )
            session.add(employee)
            session.flush()
            visitor.state = "converted"
            visitor.local_employee_id = employee.id
            visitor.updated_at = utc_now()
            session.commit()
            session.refresh(employee)
            return employee
        except Exception:
            session.rollback()
            if destination is not None:
                destination.unlink(missing_ok=True)
            raise
        finally:
            session.close()

    def status(self) -> dict[str, int | str]:
        session = self.Session()
        try:
            return {
                "pending": session.query(UnknownFaceObservation).filter(UnknownFaceObservation.processing_status == "pending").count(),
                "clustered": session.query(UnknownFaceObservation).filter(UnknownFaceObservation.processing_status == "clustered").count(),
                "active_visitors": session.query(UnknownVisitor).filter(UnknownVisitor.state == "active").count(),
            }
        finally:
            session.close()


def _write_status(payload: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(STATUS_FILE)


def run_clusterer() -> None:
    settings = load_unknown_visitor_settings()
    service = UnknownVisitorService(settings=settings)
    stopping = False

    def stop_handler(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    last_cleanup = 0.0
    logger.info("Unknown visitor clusterer started enabled=%s", settings.enabled)
    while not stopping:
        backfilled = service.backfill_missing()
        clustered = service.process_pending()
        now = time.monotonic()
        purged = service.purge_expired() if now - last_cleanup >= 86400 else 0
        if purged or now - last_cleanup >= 86400:
            last_cleanup = now
        _write_status({"running": True, "updated_at": utc_now().isoformat(), "backfilled": backfilled, "clustered": clustered, **service.status()})
        time.sleep(settings.poll_interval_seconds)
    _write_status({"running": False, "updated_at": utc_now().isoformat(), **service.status()})


if __name__ == "__main__":
    run_clusterer()
