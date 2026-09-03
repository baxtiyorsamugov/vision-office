from sqlalchemy import Boolean, Column, Integer, String, DateTime, Float, ForeignKey, JSON, Text
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

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
    timestamp = Column(DateTime, default=datetime.now)
    event_type = Column(String(20))

class Violation(Base):
    __tablename__ = 'violations'
    
    id = Column(Integer, primary_key=True)
    camera_id = Column(String(50))
    violation_type = Column(String(50)) # "trash_detected"
    screenshot_path = Column(String(255))
    timestamp = Column(DateTime, default=datetime.now)


class RemotePerson(Base):
    """Server-owned person cached locally for offline face matching."""
    __tablename__ = 'remote_persons'

    id = Column(String(36), primary_key=True)
    person_type = Column(String(20), nullable=False)
    fio = Column(String(255), nullable=True)
    embedding = Column(JSON, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class EdgeSyncState(Base):
    __tablename__ = 'edge_sync_state'

    id = Column(Integer, primary_key=True, default=1)
    last_full_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_incremental_sync_at = Column(DateTime(timezone=True), nullable=True)
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
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
