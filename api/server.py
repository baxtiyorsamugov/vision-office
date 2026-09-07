"""Read-only integration API for Vision Office operations and analytics."""

import hmac
import json
import os
from pathlib import Path
from datetime import date, datetime, time, timedelta
from typing import Generator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, text
from sqlalchemy.orm import Session, sessionmaker

from database.manager import get_engine
from database.migrations import run_migrations
from database.models import Attendance, Employee, HealthIncident, RecognitionEvent
from core.performance import PROJECT_ROOT, read_runtime_status


engine = get_engine()
run_migrations(engine)
SessionLocal = sessionmaker(bind=engine)
API_KEY = os.getenv("VISION_OFFICE_API_KEY", "")
CORS_ORIGINS = [origin.strip() for origin in os.getenv("VISION_OFFICE_CORS_ORIGINS", "").split(",") if origin.strip()]

app = FastAPI(
    title="Vision Office Integration API",
    version="1.1.0",
    description="Read-only API for employees, recognition events and Edge health.",
)

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["X-API-Key", "Content-Type"],
    )


class EmployeeResponse(BaseModel):
    id: int
    full_name: str
    role: str | None

    model_config = ConfigDict(from_attributes=True)


class AttendanceResponse(BaseModel):
    id: int
    employee_id: int
    full_name: str
    role: str | None
    event_type: str | None
    timestamp: datetime


class AttendanceSummary(BaseModel):
    date: date
    total_events: int
    unique_employees: int
    on_time: int
    late: int


class PaginatedEmployees(BaseModel):
    items: list[EmployeeResponse]
    limit: int
    offset: int
    total: int


class PaginatedAttendance(BaseModel):
    items: list[AttendanceResponse]
    limit: int
    offset: int
    total: int


class RecognitionEventResponse(BaseModel):
    id: str
    camera_id: str
    event_type: str
    person_id: str | None
    person_type: str
    person_name: str | None
    confidence: float | None
    photo_path: str | None
    created_at: datetime


class IncidentResponse(BaseModel):
    id: str
    component: str
    status: str
    message: str
    opened_at: datetime
    recovered_at: datetime | None


class PaginatedRecognitionEvents(BaseModel):
    items: list[RecognitionEventResponse]
    limit: int
    offset: int
    total: int


class PaginatedIncidents(BaseModel):
    items: list[IncidentResponse]
    limit: int
    offset: int
    total: int


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def day_bounds(selected_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(selected_date, time.min)
    return start, start + timedelta(days=1)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if API_KEY and not (x_api_key and hmac.compare_digest(x_api_key, API_KEY)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


@app.get("/", tags=["service"])
def root() -> dict[str, str]:
    return {"service": "Vision Office Integration API", "docs": "/docs"}


@app.get("/api/v1/health", tags=["service"])
def health(session: Session = Depends(get_session)) -> dict[str, object]:
    session.execute(text("SELECT 1"))
    return {"status": "ok", "timestamp": datetime.now().isoformat(), "runtime": read_runtime_status()}


@app.get("/api/v1/status", tags=["service"])
def runtime_status(_: None = Depends(require_api_key)) -> dict[str, object]:
    health_file = PROJECT_ROOT / "data" / "health_status.json"
    try:
        health_status = json.loads(health_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        health_status = None
    return {"runtime": read_runtime_status(), "health": health_status}


@app.get("/api/v1/employees", response_model=PaginatedEmployees, tags=["employees"])
def list_employees(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    _: None = Depends(require_api_key),
) -> PaginatedEmployees:
    total = session.query(Employee).count()
    employees = session.query(Employee).order_by(Employee.id.asc()).offset(offset).limit(limit).all()
    return PaginatedEmployees(
        items=[EmployeeResponse.model_validate(employee) for employee in employees],
        limit=limit,
        offset=offset,
        total=total,
    )


@app.get("/api/v1/attendance", response_model=PaginatedAttendance, tags=["attendance"])
def list_attendance(
    selected_date: date | None = Query(default=None, alias="date"),
    employee_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    _: None = Depends(require_api_key),
) -> PaginatedAttendance:
    query = session.query(Attendance, Employee).join(Employee, Attendance.employee_id == Employee.id)
    if selected_date:
        day_start, day_end = day_bounds(selected_date)
        query = query.filter(Attendance.timestamp >= day_start, Attendance.timestamp < day_end)
    if employee_id:
        query = query.filter(Attendance.employee_id == employee_id)

    total = query.count()
    rows = query.order_by(Attendance.timestamp.desc()).offset(offset).limit(limit).all()
    return PaginatedAttendance(
        items=[
            AttendanceResponse(
                id=attendance.id,
                employee_id=attendance.employee_id,
                full_name=employee.full_name,
                role=employee.role,
                event_type=attendance.event_type,
                timestamp=attendance.timestamp,
            )
            for attendance, employee in rows
        ],
        limit=limit,
        offset=offset,
        total=total,
    )


@app.get("/api/v1/attendance/summary", response_model=AttendanceSummary, tags=["attendance"])
def attendance_summary(
    selected_date: date = Query(alias="date"),
    session: Session = Depends(get_session),
    _: None = Depends(require_api_key),
) -> AttendanceSummary:
    day_start, day_end = day_bounds(selected_date)
    events = session.query(Attendance).filter(
        Attendance.timestamp >= day_start, Attendance.timestamp < day_end,
    ).all()
    work_start = datetime.combine(selected_date, time(hour=9))
    return AttendanceSummary(
        date=selected_date,
        total_events=len(events),
        unique_employees=len({event.employee_id for event in events}),
        on_time=sum(event.timestamp <= work_start for event in events),
        late=sum(event.timestamp > work_start for event in events),
    )


@app.get("/api/v1/recognition-events", response_model=PaginatedRecognitionEvents, tags=["recognition"])
def list_recognition_events(
    camera_id: str | None = Query(default=None),
    person_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    _: None = Depends(require_api_key),
) -> PaginatedRecognitionEvents:
    query = session.query(RecognitionEvent)
    if camera_id:
        query = query.filter(RecognitionEvent.camera_id == camera_id)
    if person_id:
        query = query.filter(RecognitionEvent.person_id == person_id)
    total = query.count()
    rows = query.order_by(RecognitionEvent.created_at.desc()).offset(offset).limit(limit).all()
    return PaginatedRecognitionEvents(
        items=[RecognitionEventResponse(
            id=row.id, camera_id=row.camera_id, event_type=row.event_type,
            person_id=row.person_id, person_type=row.person_type, person_name=row.person_name,
            confidence=row.confidence, photo_path=row.photo_path, created_at=row.created_at,
        ) for row in rows],
        limit=limit, offset=offset, total=total,
    )


@app.get("/api/v1/incidents", response_model=PaginatedIncidents, tags=["health"])
def list_incidents(
    active_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    _: None = Depends(require_api_key),
) -> PaginatedIncidents:
    query = session.query(HealthIncident)
    if active_only:
        query = query.filter(HealthIncident.status == "open")
    total = query.count()
    rows = query.order_by(HealthIncident.opened_at.desc()).offset(offset).limit(limit).all()
    return PaginatedIncidents(
        items=[IncidentResponse(
            id=row.id, component=row.component, status=row.status, message=row.message,
            opened_at=row.opened_at, recovered_at=row.recovered_at,
        ) for row in rows],
        limit=limit, offset=offset, total=total,
    )
