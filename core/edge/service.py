from __future__ import annotations

import json
import hashlib
import math
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

from core.edge.config import EdgeSettings
from database.manager import get_engine
from database.models import AccessLogOutbox, EdgeSyncState, RemotePerson, RemotePersonReferencePhoto, RecognitionEvent


PERSON_TYPES = {"employee", "teacher", "student"}
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
TERMINAL_STATUS_CODES = {400, 401, 403, 404, 409, 422}
PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("vision_office.edge")


class EdgeService:
    """Keeps a local matching cache and reliably forwards access events."""

    def __init__(self, settings: EdgeSettings, engine=None):
        self.settings = settings
        self.engine = engine or get_engine()
        # Upload UI needs the created reference-photo identifier after commit.
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        from database.migrations import run_migrations

        run_migrations(self.engine)
        self._recognizer = None

    def _state(self, session) -> EdgeSyncState:
        state = session.get(EdgeSyncState, 1)
        if state is None:
            state = EdgeSyncState(id=1)
            session.add(state)
            session.flush()
        return state

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """Accept legacy SQLite UTC values and PostgreSQL timezone-aware values."""
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def maintenance(self) -> None:
        """Run a due synchronization and one delivery pass without raising to video code."""
        if not self.settings.configured:
            return
        self.sync_if_due()
        self.deliver_due_events()

    def sync_if_due(self) -> bool:
        if not self.settings.configured:
            return False
        session = self.Session()
        try:
            state = self._state(session)
            now = self._utcnow()
            full_due = state.last_full_sync_at is None or now - self._as_utc(state.last_full_sync_at) >= timedelta(seconds=self.settings.full_sync_interval_seconds)
            incremental_due = state.last_incremental_sync_at is None or now - self._as_utc(state.last_incremental_sync_at) >= timedelta(seconds=self.settings.sync_interval_seconds)
            if not full_due and not incremental_due:
                return False
            since = None if full_due else state.last_incremental_sync_at
        finally:
            session.close()

        try:
            people = self._fetch_people(since)
        except EdgeRequestError as error:
            self._record_sync_error(str(error))
            return False

        session = self.Session()
        try:
            state = self._state(session)
            for person in people:
                self._upsert_person(session, person)
            completed_at = self._utcnow()
            state.last_incremental_sync_at = completed_at
            if since is None:
                state.last_full_sync_at = completed_at
            state.last_error = None
            session.commit()
            return True
        except Exception as error:
            session.rollback()
            self._record_sync_error(f"Cache update failed: {error}")
            return False
        finally:
            session.close()

    def _fetch_people(self, updated_since: datetime | None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        offset = 0
        while True:
            query: dict[str, str | int] = {"limit": self.settings.page_size, "offset": offset}
            if updated_since:
                query["updated_since"] = self._as_utc(updated_since).isoformat().replace("+00:00", "Z")
            payload = self._request_json("GET", "/api/v1/learning-centers/persons/sync", query=query)
            if not isinstance(payload, list):
                raise EdgeRequestError("Sync response must be a JSON list")
            result.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < self.settings.page_size:
                return result
            offset += self.settings.page_size

    def _upsert_person(self, session, payload: dict[str, Any]) -> None:
        person_id = str(payload.get("id", ""))
        person_type = payload.get("person_type")
        if not person_id or person_type not in PERSON_TYPES:
            return
        embedding = payload.get("embedding")
        person = session.get(RemotePerson, person_id)
        if person is None:
            person = RemotePerson(id=person_id)
            session.add(person)
        person.person_type = person_type
        # Production sync currently returns a compact payload.  Never let that
        # response erase identity metadata or a locally generated embedding.
        incoming_name = str(payload.get("fio") or payload.get("full_name") or "").strip()
        if incoming_name:
            person.fio = incoming_name
        person.active = bool(payload.get("active", True))
        incoming_photo_url = str(payload.get("person_photo_url") or payload.get("photo_url") or "").strip()
        if incoming_photo_url:
            person.photo_url = incoming_photo_url
        if self._valid_embedding(embedding):
            normalized = self._normalize_embedding(embedding)
            person.embedding = normalized.tolist()
            person.embedding_status = "ready"
            person.embedding_error = None
        elif not self._valid_embedding(person.embedding):
            person.embedding = None
            person.embedding_status = "pending"
            person.embedding_error = "ERP embedding is missing or invalid; photo fallback is required"
            self._create_embedding_from_photo(person)
        person.updated_at = self._utcnow()

    @staticmethod
    def _valid_embedding(embedding: Any) -> bool:
        return (
            isinstance(embedding, list)
            and len(embedding) == 512
            and all(isinstance(value, (int, float)) and math.isfinite(value) for value in embedding)
        )

    @staticmethod
    def _normalize_embedding(embedding: Any) -> np.ndarray:
        vector = np.asarray(embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        if norm <= 0 or not math.isfinite(norm):
            raise ValueError("Embedding norm must be positive and finite")
        return vector / norm

    def _create_embedding_from_photo(self, person: RemotePerson) -> None:
        if not person.photo_url:
            person.embedding_status = "invalid"
            person.embedding_error = "No valid embedding or person_photo_url supplied by ERP"
            return
        try:
            photo, photo_path = self._download_reference_photo(person.id, person.photo_url)
            person.photo_path = photo_path
            if self._recognizer is None:
                from core.ai.recognizer import FaceRecognizer
                self._recognizer = FaceRecognizer()
            embedding = self._recognizer.get_embedding(photo)
            if embedding is None or not self._valid_embedding(np.asarray(embedding).tolist()):
                raise ValueError("No usable face detected in reference photo")
            normalized = self._normalize_embedding(embedding)
            person.embedding = normalized.tolist()
            person.embedding_status = "ready"
            person.embedding_error = None
        except Exception as error:
            person.embedding_status = "invalid"
            person.embedding_error = str(error)[:500]
            logger.warning("Reference photo rejected person_id=%s error=%s", person.id, error)

    def _download_reference_photo(self, person_id: str, photo_url: str) -> tuple[np.ndarray, str]:
        request = Request(photo_url, headers={"Authorization": f"Bearer {self.settings.device_api_key}"})
        try:
            with urlopen(request, timeout=self.settings.request_timeout_seconds) as response:
                raw = response.read()
        except (HTTPError, URLError, OSError) as error:
            raise ValueError(f"Reference photo download failed: {error}") from error
        import cv2

        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            raise ValueError("Reference photo is not a decodable image")
        target = PROJECT_ROOT / "data" / "persons"
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{person_id}.jpg"
        if not cv2.imwrite(str(path), image):
            raise ValueError("Could not persist reference photo")
        return image, str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")

    def _local_reference_photo_path(self, person_id: str, checksum: str, record_id: str) -> Path:
        person_key = hashlib.sha256(person_id.encode("utf-8")).hexdigest()[:16]
        target = PROJECT_ROOT / "data" / "persons" / "local" / person_key
        target.mkdir(parents=True, exist_ok=True)
        return target / f"{checksum[:16]}-{record_id[:8]}.jpg"

    def add_local_reference_photo(self, person_id: str, raw_photo: bytes) -> RemotePersonReferencePhoto:
        """Persist one operator-approved reference photo without changing ERP data.

        Every accepted image adds a separate normalized vector to matching. The
        server-owned main photo and embedding remain untouched, so the next ERP
        sync cannot erase local enrichment.
        """
        if not self.settings.configured:
            raise ValueError("ERP integration must be configured before adding a local reference photo")
        if not raw_photo or len(raw_photo) > 10 * 1024 * 1024:
            raise ValueError("Reference photo must be a non-empty image no larger than 10 MB")
        checksum = hashlib.sha256(raw_photo).hexdigest()

        session = self.Session()
        try:
            person = session.get(RemotePerson, person_id)
            if person is None:
                raise ValueError("Person is not present in the local ERP cache")
            if not person.active:
                raise ValueError("Cannot add a reference photo for an inactive person")
            duplicate = session.query(RemotePersonReferencePhoto).filter(
                RemotePersonReferencePhoto.person_id == person_id,
                RemotePersonReferencePhoto.image_checksum == checksum,
            ).first()
            if duplicate is not None:
                raise ValueError("This reference photo has already been added")
            existing_count = session.query(RemotePersonReferencePhoto).filter(
                RemotePersonReferencePhoto.person_id == person_id,
                RemotePersonReferencePhoto.active.is_(True),
            ).count()
            if existing_count >= self.settings.local_reference_photo_limit:
                raise ValueError(f"Reference photo limit reached ({self.settings.local_reference_photo_limit})")
        finally:
            session.close()

        import cv2

        image = cv2.imdecode(np.frombuffer(raw_photo, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            raise ValueError("Reference photo is not a decodable image")
        if self._recognizer is None:
            from core.ai.recognizer import FaceRecognizer

            self._recognizer = FaceRecognizer()
        embedding = self._recognizer.get_embedding(image)
        if embedding is None or not self._valid_embedding(np.asarray(embedding).tolist()):
            raise ValueError("No usable face detected in the reference photo")

        record_id = str(uuid.uuid4())
        path = self._local_reference_photo_path(person_id, checksum, record_id)
        if not cv2.imwrite(str(path), image):
            raise ValueError("Could not persist the reference photo")

        session = self.Session()
        try:
            record = RemotePersonReferencePhoto(
                id=record_id,
                person_id=person_id,
                source="local",
                photo_path=str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                image_checksum=checksum,
                embedding=self._normalize_embedding(embedding).tolist(),
                active=True,
                created_at=self._utcnow(),
            )
            session.add(record)
            session.commit()
            return record
        except (IntegrityError, ValueError):
            session.rollback()
            path.unlink(missing_ok=True)
            raise
        except Exception:
            session.rollback()
            path.unlink(missing_ok=True)
            raise
        finally:
            session.close()

    def cache_embeddings(self) -> tuple[list[dict[str, str]], np.ndarray | None]:
        session = self.Session()
        try:
            people = session.query(RemotePerson).filter(
                RemotePerson.active.is_(True), RemotePerson.embedding.is_not(None)
            ).all()
            identities: list[dict[str, str]] = []
            vectors: list[np.ndarray] = []
            people_by_id = {person.id: person for person in people}
            for person in people:
                if not self._valid_embedding(person.embedding):
                    continue
                vector = np.asarray(person.embedding, dtype=np.float32)
                norm = np.linalg.norm(vector)
                if norm == 0:
                    continue
                identities.append({
                    "person_id": person.id,
                    "person_type": person.person_type,
                    "name": person.fio or f"{person.person_type} {person.id[:8]}",
                })
                vectors.append(vector / norm)
            if people_by_id:
                extra_photos = session.query(RemotePersonReferencePhoto).filter(
                    RemotePersonReferencePhoto.person_id.in_(people_by_id),
                    RemotePersonReferencePhoto.active.is_(True),
                    RemotePersonReferencePhoto.embedding.is_not(None),
                ).all()
                for photo in extra_photos:
                    if not self._valid_embedding(photo.embedding):
                        continue
                    vector = np.asarray(photo.embedding, dtype=np.float32)
                    norm = np.linalg.norm(vector)
                    if norm == 0:
                        continue
                    person = people_by_id[photo.person_id]
                    identities.append({
                        "person_id": person.id,
                        "person_type": person.person_type,
                        "name": person.fio or f"{person.person_type} {person.id[:8]}",
                    })
                    vectors.append(vector / norm)
            return identities, np.vstack(vectors) if vectors else None
        finally:
            session.close()

    def queue_access_event(self, event: RecognitionEvent) -> bool:
        """Queue a recognized employee event. Unknown events deliberately remain local."""
        if not self.settings.configured or not event.person_id or event.person_type != "employee":
            return False
        now = datetime.now(ZoneInfo(self.settings.timezone))
        signature = f"{event.person_id}:{event.camera_id}:{event.event_type}"
        session = self.Session()
        try:
            recent_after = self._utcnow() - timedelta(seconds=self.settings.event_cooldown_seconds)
            recent = session.query(AccessLogOutbox).filter(
                AccessLogOutbox.subject_signature == signature,
                AccessLogOutbox.created_at >= recent_after,
            ).first()
            if recent:
                return False
            event_time = event.created_at or self._utcnow()
            body: dict[str, Any] = {
                "event_id": event.id,
                "edge_device_id": self.settings.device_id,
                "person_id": event.person_id,
                "person_type": event.person_type,
                "event_type": event.event_type,
                "event_time": event_time.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(self.settings.timezone)).isoformat(),
                "camera_id": event.camera_id,
                "similarity_score": event.confidence,
                "camera_photo_path": event.photo_path,
            }
            session.add(AccessLogOutbox(
                id=event.id,
                idempotency_key=f"{self.settings.device_id}:{event.id}",
                subject_signature=signature,
                payload=json.dumps(body, separators=(",", ":")),
                status="pending",
                next_attempt_at=self._utcnow(),
                endpoint="/api/v1/learning-centers/access-logs",
            ))
            session.commit()
            return True
        finally:
            session.close()

    def queue_heartbeat(self, payload: dict[str, Any]) -> bool:
        if not self.settings.configured:
            return False
        event_id = str(uuid.uuid4())
        session = self.Session()
        try:
            session.add(AccessLogOutbox(
                id=event_id,
                idempotency_key=f"{self.settings.device_id}:heartbeat:{event_id}",
                subject_signature="heartbeat",
                payload=json.dumps(payload, separators=(",", ":")),
                status="pending",
                next_attempt_at=self._utcnow(),
                endpoint="/api/v1/edge/heartbeat",
            ))
            session.commit()
            return True
        finally:
            session.close()

    def queue_recognition(self, identity: dict[str, str] | None, confidence: float | None, embedding: np.ndarray, subject_hint: str | None = None) -> bool:
        """Compatibility wrapper retained for integrations written before event storage."""
        if identity is None:
            return False
        event = RecognitionEvent(
            id=str(uuid.uuid4()), camera_id="legacy", event_type="entry",
            person_id=identity.get("person_id"), person_type=identity.get("person_type", "employee"),
            person_name=identity.get("name"), subject_signature=identity.get("person_id") or subject_hint or "legacy",
            confidence=confidence, created_at=self._utcnow(),
        )
        return self.queue_access_event(event)

    def deliver_due_events(self, limit: int = 25) -> int:
        if not self.settings.configured:
            return 0
        session = self.Session()
        try:
            now = self._utcnow()
            events = session.query(AccessLogOutbox).filter(
                AccessLogOutbox.status.in_(["pending", "retry"]),
                AccessLogOutbox.next_attempt_at <= now,
            ).order_by(AccessLogOutbox.created_at.asc()).limit(limit).all()
            event_ids = [event.id for event in events]
        finally:
            session.close()
        delivered = 0
        for event_id in event_ids:
            if self._deliver_event(event_id):
                delivered += 1
        return delivered

    def _deliver_event(self, event_id: str) -> bool:
        session = self.Session()
        try:
            event = session.get(AccessLogOutbox, event_id)
            if event is None or event.status not in {"pending", "retry"}:
                return False
            try:
                self._request_json("POST", event.endpoint, body=json.loads(event.payload), headers={"Idempotency-Key": event.idempotency_key})
            except EdgeRequestError as error:
                event.attempts += 1
                event.last_error = str(error)
                if error.status_code in TERMINAL_STATUS_CODES:
                    event.status = "failed"
                    event.next_attempt_at = None
                else:
                    event.status = "retry"
                    event.next_attempt_at = self._utcnow() + timedelta(seconds=min(300, 2 ** min(event.attempts, 8)))
                session.commit()
                return False
            event.status = "sent"
            event.sent_at = self._utcnow()
            event.last_error = None
            session.commit()
            return True
        finally:
            session.close()

    def _record_sync_error(self, message: str) -> None:
        session = self.Session()
        try:
            self._state(session).last_error = message
            session.commit()
        finally:
            session.close()

    def _request_json(self, method: str, path: str, query: dict[str, str | int] | None = None, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        url = f"{self.settings.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        request_headers = {"Authorization": f"Bearer {self.settings.device_api_key}", "Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=self.settings.request_timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
                return json.loads(response_body) if response_body else None
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise EdgeRequestError(f"HTTP {error.code}: {detail}", error.code) from error
        except (URLError, OSError, ValueError) as error:
            raise EdgeRequestError(f"Network error: {error}") from error


class EdgeRequestError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
