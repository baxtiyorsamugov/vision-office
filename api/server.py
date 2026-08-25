"""Read-only integration API for Vision Office."""

import hmac
import os
from datetime import date, datetime, time
from typing import Generator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, text
from sqlalchemy.orm import Session, sessionmaker

from database.manager import get_engine
from database.models import Attendance, Employee


engine = get_engine()
SessionLocal = sessionmaker(bind=engine)
API_KEY = os.getenv("VISION_OFFICE_API_KEY", "")
CORS_ORIGINS = [origin.strip() for origin in os.getenv("VISION_OFFICE_CORS_ORIGINS", "").split(",") if origin.strip()]

app = FastAPI(
    title="Vision Office Integration API",
    version="1.0.0",
    description="Read-only API for employees and attendance events.",
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


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


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
def health(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


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
        query = query.filter(func.date(Attendance.timestamp) == selected_date.isoformat())
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
    events = session.query(Attendance).filter(func.date(Attendance.timestamp) == selected_date.isoformat()).all()
    work_start = datetime.combine(selected_date, time(hour=9))
    return AttendanceSummary(
        date=selected_date,
        total_events=len(events),
        unique_employees=len({event.employee_id for event in events}),
        on_time=sum(event.timestamp <= work_start for event in events),
        late=sum(event.timestamp > work_start for event in events),
    )
