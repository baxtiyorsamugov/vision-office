from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy.orm import sessionmaker

from core.edge.config import EdgeSettings
from database.manager import get_engine
from database.models import AccessLogOutbox, EdgeSyncState, RemotePerson


PERSON_TYPES = {"employee", "teacher", "student"}
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class EdgeService:
    """Keeps a local matching cache and reliably forwards access events."""

    def __init__(self, settings: EdgeSettings, engine=None):
        self.settings = settings
        self.engine = engine or get_engine()
        self.Session = sessionmaker(bind=self.engine)
        # Existing deployments have no migration runner; create only new tables.
        RemotePerson.__table__.create(self.engine, checkfirst=True)
        EdgeSyncState.__table__.create(self.engine, checkfirst=True)
        AccessLogOutbox.__table__.create(self.engine, checkfirst=True)

    def _state(self, session) -> EdgeSyncState:
        state = session.get(EdgeSyncState, 1)
        if state is None:
            state = EdgeSyncState(id=1)
            session.add(state)
            session.flush()
        return state

    @staticmethod
    def _utcnow() -> datetime:
        # SQLite does not preserve timezone metadata. Store scheduler values as naive UTC.
        return datetime.utcnow()

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
            full_due = state.last_full_sync_at is None or now - state.last_full_sync_at >= timedelta(seconds=self.settings.full_sync_interval_seconds)
            incremental_due = state.last_incremental_sync_at is None or now - state.last_incremental_sync_at >= timedelta(seconds=self.settings.sync_interval_seconds)
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
                query["updated_since"] = updated_since.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
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
        if embedding is not None and not self._valid_embedding(embedding):
            raise ValueError(f"Person {person_id} has an invalid embedding")
        person = session.get(RemotePerson, person_id)
        if person is None:
            person = RemotePerson(id=person_id)
            session.add(person)
        person.person_type = person_type
        person.fio = str(payload.get("fio") or payload.get("full_name") or "") or None
        person.embedding = embedding
        person.active = bool(payload.get("active", True))
        person.updated_at = self._utcnow()

    @staticmethod
    def _valid_embedding(embedding: Any) -> bool:
        return (
            isinstance(embedding, list)
            and len(embedding) == 512
            and all(isinstance(value, (int, float)) and math.isfinite(value) for value in embedding)
        )

    def cache_embeddings(self) -> tuple[list[dict[str, str]], np.ndarray | None]:
        session = self.Session()
        try:
            people = session.query(RemotePerson).filter(
                RemotePerson.active.is_(True), RemotePerson.embedding.is_not(None)
            ).all()
            identities: list[dict[str, str]] = []
            vectors: list[np.ndarray] = []
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
            return identities, np.vstack(vectors) if vectors else None
        finally:
            session.close()

    def queue_recognition(self, identity: dict[str, str] | None, confidence: float | None, embedding: np.ndarray, subject_hint: str | None = None) -> bool:
        """Queue a known or unknown entry event, respecting a local cooldown."""
        if not self.settings.configured:
            return False
        if identity is None and len(embedding) != 512:
            return False
        now = datetime.now(ZoneInfo(self.settings.timezone))
        signature = identity["person_id"] if identity else (subject_hint or sha256(np.asarray(embedding, dtype=np.float32).tobytes()).hexdigest())
        session = self.Session()
        try:
            recent_after = self._utcnow() - timedelta(seconds=self.settings.event_cooldown_seconds)
            recent = session.query(AccessLogOutbox).filter(
                AccessLogOutbox.subject_signature == signature,
                AccessLogOutbox.created_at >= recent_after,
            ).first()
            if recent:
                return False
            body: dict[str, Any] = {
                "event_type": "entry",
                "person_type": identity["person_type"] if identity else "unknown",
                "occurred_at": now.isoformat(),
            }
            if confidence is not None:
                body["confidence"] = round(max(0.0, min(1.0, confidence)), 6)
            if identity:
                body["person_id"] = identity["person_id"]
            else:
                body["embedding"] = np.asarray(embedding, dtype=np.float32).tolist()
            event_id = str(uuid.uuid4())
            session.add(AccessLogOutbox(
                id=event_id,
                idempotency_key=f"{self.settings.device_id}:{event_id}",
                subject_signature=signature,
                payload=json.dumps(body, separators=(",", ":")),
                status="pending",
                next_attempt_at=self._utcnow(),
            ))
            session.commit()
            return True
        finally:
            session.close()

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
                self._request_json("POST", "/api/v1/learning-centers/access-logs", body=json.loads(event.payload), headers={"Idempotency-Key": event.idempotency_key})
            except EdgeRequestError as error:
                event.attempts += 1
                event.last_error = str(error)
                if error.status_code in {401, 403, 422}:
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
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise EdgeRequestError(f"HTTP {error.code}: {detail}", error.code) from error
        except (URLError, OSError, ValueError) as error:
            raise EdgeRequestError(f"Network error: {error}") from error


class EdgeRequestError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
