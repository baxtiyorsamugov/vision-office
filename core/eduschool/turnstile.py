"""Device-local, opt-in delivery of verified staff events to EduSchool."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import errno
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml
from sqlalchemy import or_
from sqlalchemy.orm import sessionmaker

from database.manager import get_engine
from database.migrations import run_migrations
from database.models import (
    EduSchoolCatalogPerson, EduSchoolReferencePhoto, EduSchoolTurnstileOutbox,
    EduSchoolTurnstileState, RecognitionEvent,
)
from core.eduschool.photos import normalized_embedding


logger = logging.getLogger("vision_office.eduschool.turnstile")
OBJECT_ID = re.compile(r"^[0-9a-fA-F]{24}$")
TERMINAL_CODES = {10004, 10500, 10600, 51804, 55103, 55101, 422}


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _has_usable_embedding(value) -> bool:
    try:
        normalized_embedding(value)
        return True
    except (TypeError, ValueError, OverflowError):
        return False


@dataclass(frozen=True)
class TurnstileSettings:
    enabled: bool = False
    base_url: str = "https://backend.eduschool.uz"
    branch_id: str = ""
    api_key: str = field(default="", repr=False)
    device_ids: dict[str, str] = field(default_factory=dict)
    poll_interval_seconds: int = 5
    request_timeout_seconds: int = 10

    def validation_error(self) -> str | None:
        if not self.enabled:
            return None
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            return "EduSchool turnstile base_url must be an HTTPS origin"
        if not OBJECT_ID.fullmatch(self.branch_id):
            return "EduSchool turnstile branch_id is missing or invalid"
        if not self.api_key:
            return "EDUSCHOOL_TURNSTILE_API_KEY is missing"
        if not self.device_ids or any(not name or not value or len(value) > 64 for name, value in self.device_ids.items()):
            return "EduSchool turnstile device_ids are missing or invalid"
        if len(set(self.device_ids.values())) != len(self.device_ids):
            return "EduSchool turnstile device_ids must be unique"
        return None


def load_settings(path: str | Path = "config/settings.yaml") -> TurnstileSettings:
    config_path = Path(path)
    values = {}
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as source:
            values = (yaml.safe_load(source) or {}).get("eduschool_turnstile") or {}
    devices = values.get("device_ids") or {}
    return TurnstileSettings(
        enabled=bool(values.get("enabled", False)),
        base_url=str(values.get("base_url", "https://backend.eduschool.uz")).rstrip("/"),
        branch_id=str(values.get("branch_id") or "").strip(),
        api_key=os.getenv("EDUSCHOOL_TURNSTILE_API_KEY", "").strip(),
        device_ids={str(key).strip(): str(value).strip() for key, value in devices.items()} if isinstance(devices, dict) else {},
        poll_interval_seconds=max(1, min(60, int(values.get("poll_interval_seconds", 5)))),
        request_timeout_seconds=max(1, min(60, int(values.get("request_timeout_seconds", 10)))),
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    code: int | None = None
    backend_id: str | None = None
    duplicate: bool = False
    reason: str | None = None


def send_attendance(settings: TurnstileSettings, payload: dict) -> DeliveryResult:
    """Never redirect the API key; interpret EduSchool's envelope, not HTTP 400 text."""
    request = Request(
        f"{settings.base_url}/external-api/turnstile/attendance",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"apikey": settings.api_key, "branch": settings.branch_id, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=settings.request_timeout_seconds) as response:
            status = response.status
            body = response.read(65537)
    except HTTPError as error:
        status = error.code
        body = error.read(65537)
    except (TimeoutError, socket.timeout):
        return DeliveryResult("ambiguous", reason="Request timed out after submission")
    except URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            return DeliveryResult("ambiguous", reason="Request timed out after submission")
        if isinstance(error.reason, socket.gaierror) or (
            isinstance(error.reason, OSError) and error.reason.errno in (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH)
        ):
            return DeliveryResult("retry", reason=type(error.reason).__name__)
        return DeliveryResult("ambiguous", reason=type(error.reason).__name__)
    except OSError as error:
        return DeliveryResult("ambiguous", reason=type(error).__name__)
    if len(body) > 65536:
        return DeliveryResult("ambiguous", reason="Oversized response")
    try:
        envelope = json.loads(body)
        code = envelope.get("code")
        data = envelope.get("data") or {}
        if status == 200 and code == 0 and isinstance(data, dict):
            return DeliveryResult("sent", code=0, backend_id=str(data.get("_id") or "")[:64] or None,
                                  duplicate=data.get("duplicate") is True)
        if isinstance(code, int) and code in TERMINAL_CODES:
            return DeliveryResult("blocked", code=code, reason=f"EduSchool code {code}")
    except (ValueError, TypeError, AttributeError):
        pass
    if status >= 500:
        return DeliveryResult("retry", reason=f"HTTP {status}")
    return DeliveryResult("ambiguous" if status == 200 else "blocked", reason=f"Unexpected HTTP {status} response")


class EduSchoolTurnstileService:
    def __init__(self, settings: TurnstileSettings, engine=None):
        self.settings = settings
        self.engine = engine if engine is not None else get_engine()
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def prepare_activation(self) -> bool:
        error = self.settings.validation_error()
        with self.Session.begin() as session:
            state = session.get(EduSchoolTurnstileState, 1)
            if state is None:
                state = EduSchoolTurnstileState(id=1)
                session.add(state)
            if not self.settings.enabled or error:
                if state.enabled:
                    session.query(EduSchoolTurnstileOutbox).filter(
                        EduSchoolTurnstileOutbox.status.in_(("pending", "retry"))
                    ).update({"status": "blocked", "last_error": "Integration disabled", "next_attempt_at": None}, synchronize_session=False)
                session.query(EduSchoolTurnstileOutbox).filter_by(status="sending").update(
                    {"status": "ambiguous", "last_error": "Worker stopped during request; reconcile before retry"},
                    synchronize_session=False,
                )
                state.enabled = False
                state.last_error = error
                return False
            if not state.enabled:
                state.activated_at = datetime.now(timezone.utc)
            session.query(EduSchoolTurnstileOutbox).filter_by(status="sending").update(
                {"status": "ambiguous", "last_error": "Worker stopped during request; reconcile before retry"},
                synchronize_session=False,
            )
            state.enabled = True
            state.last_error = None
        return True

    def _eligible(self, session, event: RecognitionEvent, person: EduSchoolCatalogPerson | None) -> str | None:
        if person is None or not person.active or person.person_type != "employee":
            return "Person is not an active EduSchool employee"
        if person.attendance_blocked:
            return "Attendance delivery is paused for this person"
        if not person.employee_no or not person.attendance_approved or person.attendance_approved_at is None:
            return "Employee number or automatic qualification is missing"
        if as_utc(event.created_at) < as_utc(person.attendance_approved_at):
            return "Event predates automatic qualification"
        if session.query(EduSchoolCatalogPerson.id).filter(
            EduSchoolCatalogPerson.employee_no == person.employee_no,
            EduSchoolCatalogPerson.id != person.id,
            EduSchoolCatalogPerson.active.is_(True),
        ).first():
            return "Employee number belongs to multiple active profiles"
        photos = session.query(EduSchoolReferencePhoto.embedding).filter_by(person_id=person.id, active=True).all()
        if not any(_has_usable_embedding(row[0]) for row in photos):
            return "Person has no active FaceID photo"
        return None

    def refresh_auto_approvals(self) -> int:
        """Qualify only active staff with a unique number and an enrolled face."""
        with self.Session.begin() as session:
            state = session.get(EduSchoolTurnstileState, 1)
            if state is None or not state.enabled:
                return 0
            people = session.query(EduSchoolCatalogPerson).filter_by(person_type="employee").all()
            numbers = Counter(person.employee_no for person in people if person.active and person.employee_no)
            employee_ids = [person.id for person in people]
            photos = session.query(EduSchoolReferencePhoto.person_id, EduSchoolReferencePhoto.embedding).filter(
                EduSchoolReferencePhoto.active.is_(True),
                EduSchoolReferencePhoto.person_id.in_(employee_ids),
            ).all() if employee_ids else []
            photo_ids = {person_id for person_id, embedding in photos if _has_usable_embedding(embedding)}
            now = datetime.now(timezone.utc)
            changed = 0
            for person in people:
                qualified = bool(
                    person.active and not person.attendance_blocked and person.employee_no
                    and numbers[person.employee_no] == 1 and person.id in photo_ids
                )
                if qualified and (not person.attendance_approved or person.attendance_approved_at is None):
                    person.attendance_approved = True
                    person.attendance_approved_at = now
                    changed += 1
                elif not qualified and (person.attendance_approved or person.attendance_approved_at is not None):
                    person.attendance_approved = False
                    person.attendance_approved_at = None
                    session.query(EduSchoolTurnstileOutbox).filter(
                        EduSchoolTurnstileOutbox.person_id == person.id,
                        EduSchoolTurnstileOutbox.status.in_(("pending", "retry")),
                    ).update({"status": "blocked", "last_error": "Automatic qualification lost", "next_attempt_at": None}, synchronize_session=False)
                    session.query(EduSchoolTurnstileOutbox).filter_by(
                        person_id=person.id, status="sending"
                    ).update({"status": "ambiguous", "last_error": "Qualification changed during request"}, synchronize_session=False)
                    changed += 1
            return changed


    def queue_new_events(self, limit: int = 100) -> int:
        with self.Session.begin() as session:
            state = session.get(EduSchoolTurnstileState, 1)
            if state is None or not state.enabled or state.activated_at is None:
                return 0
            events = session.query(RecognitionEvent).outerjoin(
                EduSchoolTurnstileOutbox, EduSchoolTurnstileOutbox.event_id == RecognitionEvent.id
            ).filter(
                RecognitionEvent.person_type == "eduschool_employee",
                RecognitionEvent.event_type.in_(("entry", "exit")),
                RecognitionEvent.created_at >= state.activated_at,
                EduSchoolTurnstileOutbox.event_id.is_(None),
            ).order_by(RecognitionEvent.created_at, RecognitionEvent.id).limit(limit).all()
            for event in events:
                person_id = f"employee:{event.person_id.removeprefix('edu:e:')}" if (event.person_id or "").startswith("edu:e:") else ""
                person = session.get(EduSchoolCatalogPerson, person_id) if person_id else None
                reason = self._eligible(session, event, person)
                device_id = self.settings.device_ids.get(event.camera_id)
                if not device_id:
                    reason = reason or "Camera deviceId is not configured"
                payload = None if reason else {
                    "employeeNo": person.employee_no,
                    "eventType": "check_in" if event.event_type == "entry" else "check_out",
                    "eventTime": as_utc(event.created_at).isoformat().replace("+00:00", "Z"),
                    "method": "face_recognition",
                    "deviceId": device_id,
                }
                session.add(EduSchoolTurnstileOutbox(
                    event_id=event.id, person_id=person_id or str(event.person_id or "")[:40],
                    payload=payload, status="skipped" if reason else "pending",
                    last_error=reason, next_attempt_at=datetime.now(timezone.utc) if payload else None,
                ))
            return len(events)

    def deliver_due(self, limit: int = 25) -> int:
        with self.Session() as session:
            due = session.query(EduSchoolTurnstileOutbox.event_id).filter(
                EduSchoolTurnstileOutbox.status.in_(("pending", "retry")),
                or_(EduSchoolTurnstileOutbox.next_attempt_at.is_(None), EduSchoolTurnstileOutbox.next_attempt_at <= datetime.now(timezone.utc)),
            ).order_by(EduSchoolTurnstileOutbox.created_at).limit(limit).all()
            event_ids = [row[0] for row in due]
        return sum(self._deliver_one(event_id) for event_id in event_ids)

    def _deliver_one(self, event_id: str) -> bool:
        with self.Session.begin() as session:
            state = session.get(EduSchoolTurnstileState, 1)
            item = session.get(EduSchoolTurnstileOutbox, event_id)
            if not state or not state.enabled or not item or item.status not in ("pending", "retry"):
                return False
            event = session.get(RecognitionEvent, event_id)
            person = session.get(EduSchoolCatalogPerson, item.person_id)
            reason = self._eligible(session, event, person) if event else "Recognition event is missing"
            if not reason and (item.payload or {}).get("employeeNo") != person.employee_no:
                reason = "Employee number changed after enqueue"
            if not reason and (item.payload or {}).get("deviceId") != self.settings.device_ids.get(event.camera_id):
                reason = "Camera deviceId changed after enqueue"
            if reason:
                item.status = "blocked"
                item.last_error = reason
                item.next_attempt_at = None
                return False
            payload = dict(item.payload)
            claimed = session.query(EduSchoolTurnstileOutbox).filter(
                EduSchoolTurnstileOutbox.event_id == event_id,
                EduSchoolTurnstileOutbox.status.in_(("pending", "retry")),
            ).update({"status": "sending"}, synchronize_session=False)
            if not claimed:
                return False
        result = send_attendance(self.settings, payload)
        with self.Session.begin() as session:
            item = session.get(EduSchoolTurnstileOutbox, event_id)
            if item.status != "sending":
                return False
            item.attempts += 1
            item.status = result.status
            item.response_code = result.code
            item.backend_event_id = result.backend_id
            item.duplicate = result.duplicate
            item.last_error = result.reason
            item.sent_at = datetime.now(timezone.utc) if result.status == "sent" else None
            item.next_attempt_at = (
                datetime.now(timezone.utc) + timedelta(seconds=min(300, 2 ** min(item.attempts, 8)))
                if result.status == "retry" else None
            )
        return result.status == "sent"


def set_person_hold(person_id: str, blocked: bool, engine=None) -> None:
    service = EduSchoolTurnstileService(TurnstileSettings(), engine)
    with service.Session.begin() as session:
        person = session.get(EduSchoolCatalogPerson, person_id)
        if person is None or person.person_type != "employee":
            raise ValueError("Выберите сотрудника EduSchool.")
        person.attendance_blocked = blocked
        if blocked:
            person.attendance_approved = False
            person.attendance_approved_at = None
            session.query(EduSchoolTurnstileOutbox).filter(
                EduSchoolTurnstileOutbox.person_id == person_id,
                EduSchoolTurnstileOutbox.status.in_(("pending", "retry")),
            ).update({"status": "blocked", "last_error": "Operator paused attendance delivery", "next_attempt_at": None}, synchronize_session=False)
            session.query(EduSchoolTurnstileOutbox).filter_by(
                person_id=person_id, status="sending"
            ).update({"status": "ambiguous", "last_error": "Operator paused delivery during request"}, synchronize_session=False)


def reconcile_ambiguous(event_id: str, *, already_delivered: bool, engine=None) -> None:
    """Resolve an uncertain POST only after checking the backend attendance record."""
    service = EduSchoolTurnstileService(TurnstileSettings(), engine)
    with service.Session.begin() as session:
        item = session.get(EduSchoolTurnstileOutbox, event_id)
        if item is None or item.status != "ambiguous":
            raise ValueError("Это событие не требует ручной сверки.")
        if already_delivered:
            item.status = "sent"
            item.sent_at = datetime.now(timezone.utc)
            item.last_error = "Operator confirmed delivery in EduSchool"
        else:
            item.status = "retry"
            item.next_attempt_at = datetime.now(timezone.utc)
            item.last_error = "Operator confirmed no backend record; retry authorized"
        logger.warning("EduSchool attendance event %s manually reconciled as %s", event_id, item.status)
