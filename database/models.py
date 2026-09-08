from sqlalchemy import Boolean, Column, Integer, String, DateTime, Float, ForeignKey, JSON, Text, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime, timezone

Base = declarative_base()


def utc_now() -> datetime:
    """Return an aware UTC instant for PostgreSQL TIMESTAMPTZ columns."""
    return datetime.now(timezone.utc)

class Employee(Base):
    __tablename__ = 'employees'
    
    id = Column(Integer, primary_key=True)
    full_name = Column(String(100), nullable=False)
    face_embeddings = Column(JSON, nullable=False) 
    role = Column(String(50))
    # НОВАЯ КОЛОНКА ДЛЯ ФОТО:
    photo_path = Column(String(255), default="data/faces/default.png")

class Attendance(Base):
    __tablename__ = 'attendance'
    
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey('employees.id'))
    timestamp = Column(DateTime, default=datetime.utcnow)
    event_type = Column(String(20))

class Violation(Base):
    __tablename__ = 'violations'
    
    id = Column(Integer, primary_key=True)
    camera_id = Column(String(50))
    violation_type = Column(String(50)) # "trash_detected"
    screenshot_path = Column(String(255))
    timestamp = Column(DateTime, default=datetime.utcnow)


class RemotePerson(Base):
    """Server-owned person cached locally for offline face matching."""
    __tablename__ = 'remote_persons'

    id = Column(String(36), primary_key=True)
    person_type = Column(String(20), nullable=False)
    fio = Column(String(255), nullable=True)
    embedding = Column(JSON, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    photo_url = Column(String(1024), nullable=True)
    photo_path = Column(String(1024), nullable=True)
    embedding_status = Column(String(32), nullable=False, default="pending")
    embedding_error = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class RemotePersonReferencePhoto(Base):
    """Locally approved extra photo/embedding for an ERP-managed person."""
    __tablename__ = "remote_person_reference_photos"
    __table_args__ = (UniqueConstraint("person_id", "image_checksum", name="uq_remote_person_reference_photo"),)

    id = Column(String(36), primary_key=True)
    person_id = Column(String(36), ForeignKey("remote_persons.id"), nullable=False, index=True)
    source = Column(String(20), nullable=False, default="local")
    photo_path = Column(String(1024), nullable=False)
    image_checksum = Column(String(64), nullable=False)
    embedding = Column(JSON, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class EdgeSyncState(Base):
    __tablename__ = 'edge_sync_state'

    id = Column(Integer, primary_key=True, default=1)
    last_full_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_incremental_sync_at = Column(DateTime(timezone=True), nullable=True)
    manual_full_sync_requested_at = Column(DateTime(timezone=True), nullable=True)
    last_manual_full_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)


class AccessLogOutbox(Base):
    """Persistent outgoing access-log event, safe to retry after a restart."""
    __tablename__ = 'access_log_outbox'

    id = Column(String(36), primary_key=True)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    subject_signature = Column(String(128), nullable=False, index=True)
    payload = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default='pending', index=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    endpoint = Column(String(255), nullable=False, default="/api/v1/learning-centers/access-logs")


class RecognitionEvent(Base):
    """Local immutable recognition audit event, including unknown faces."""
    __tablename__ = "recognition_events"

    id = Column(String(36), primary_key=True)
    camera_id = Column(String(100), nullable=False, index=True)
    event_type = Column(String(20), nullable=False)
    person_id = Column(String(36), nullable=True, index=True)
    person_type = Column(String(20), nullable=False, default="unknown")
    person_name = Column(String(255), nullable=True)
    subject_signature = Column(String(128), nullable=False, index=True)
    confidence = Column(Float, nullable=True)
    photo_path = Column(String(1024), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class UnknownVisitor(Base):
    """A device-local candidate identity assembled from unknown face observations."""
    __tablename__ = "unknown_visitors"

    id = Column(Integer, primary_key=True)
    state = Column(String(20), nullable=False, default="active", index=True)
    canonical_embedding = Column(JSON, nullable=True)
    primary_photo_path = Column(String(1024), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    visit_count = Column(Integer, nullable=False, default=0)
    observation_count = Column(Integer, nullable=False, default=0)
    local_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)
    merged_into_id = Column(Integer, ForeignKey("unknown_visitors.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class UnknownFaceObservation(Base):
    """A short-lived local biometric sample from an unknown recognition event."""
    __tablename__ = "unknown_face_observations"
    __table_args__ = (UniqueConstraint("event_id", name="uq_unknown_face_observation_event"),)

    id = Column(String(36), primary_key=True)
    event_id = Column(String(36), ForeignKey("recognition_events.id"), nullable=False, index=True)
    visitor_id = Column(Integer, ForeignKey("unknown_visitors.id"), nullable=True, index=True)
    embedding = Column(JSON, nullable=True)
    processing_status = Column(String(20), nullable=False, default="pending", index=True)
    processing_error = Column(Text, nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class UnknownVisitorVisit(Base):
    """One camera/direction visit, deduplicated over a bounded local time window."""
    __tablename__ = "unknown_visitor_visits"

    id = Column(String(36), primary_key=True)
    visitor_id = Column(Integer, ForeignKey("unknown_visitors.id"), nullable=False, index=True)
    camera_id = Column(String(100), nullable=False, index=True)
    event_type = Column(String(20), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    observation_count = Column(Integer, nullable=False, default=1)


class HealthIncident(Base):
    __tablename__ = "health_incidents"

    id = Column(String(36), primary_key=True)
    component = Column(String(100), nullable=False, index=True)
    status = Column(String(20), nullable=False)
    message = Column(Text, nullable=False)
    opened_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    recovered_at = Column(DateTime(timezone=True), nullable=True)
    notification_sent_at = Column(DateTime(timezone=True), nullable=True)


class NotificationOutbox(Base):
    """Persistent Telegram notification with bounded retry semantics."""
    __tablename__ = "notification_outbox"

    id = Column(String(36), primary_key=True)
    channel = Column(String(30), nullable=False)
    payload = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
