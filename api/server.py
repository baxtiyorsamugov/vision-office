"""Read-only integration API for Vision Office operations and analytics."""

import hmac
import json
import os
from pathlib import Path
from datetime import date, datetime, time, timedelta
from typing import Generator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, text
from sqlalchemy.orm import Session, sessionmaker

from database.manager import get_engine
from database.migrations import run_migrations
from database.models import Attendance, Employee, HealthIncident, RecognitionEvent
from core.performance import PROJECT_ROOT, read_runtime_status
from core.preview import preview_path
from core.config import ConfigurationError, load_app_settings


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


def monitor_camera_statuses() -> list[dict[str, object]]:
    """Return configured cameras only, ignoring stale runtime files from tests."""
    runtime = read_runtime_status() or {}
    statuses = runtime.get("cameras") or ([runtime] if runtime.get("camera_id") else [])
    by_id = {str(item.get("camera_id")): item for item in statuses if item.get("camera_id")}
    try:
        configured_ids = [camera.id for camera in load_app_settings().cameras if camera.is_active]
    except ConfigurationError:
        configured_ids = list(by_id)
    return [by_id[camera_id] for camera_id in configured_ids if camera_id in by_id]


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


@app.get("/api/v1/monitor/status", tags=["monitor"])
def monitor_status() -> dict[str, object]:
    """Local monitor status. Docker exposes the API only on 127.0.0.1."""
    cameras = monitor_camera_statuses()
    return {"running": any(bool(camera.get("running")) for camera in cameras), "cameras": cameras}


@app.get("/api/v1/cameras/{camera_id}/preview.jpg", tags=["monitor"])
def camera_preview(camera_id: str) -> FileResponse:
    """Serve the worker's cached JPEG; this endpoint never touches RTSP."""
    if camera_id not in {str(camera.get("camera_id")) for camera in monitor_camera_statuses()}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera preview is not available")
    path = preview_path(camera_id)
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview frame is not ready")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/monitor", response_class=HTMLResponse, include_in_schema=False)
def monitor_page() -> str:
    """Standalone browser monitor backed only by cached local JPEG files."""
    return """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vision Office - Monitor</title>
<style>
:root{color-scheme:dark;--bg:#111517;--surface:#1a2023;--line:#2c373b;--text:#f1f5f3;--muted:#a2aca8;--green:#29b777;--red:#ec6a5c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px Inter,Segoe UI,Arial,sans-serif}
header{height:64px;padding:0 24px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);background:#151b1e}
.brand{font-weight:700;font-size:17px}.brand b{display:inline-grid;place-items:center;width:28px;height:28px;margin-right:9px;border-radius:50%;background:var(--green);font-size:10px}.hint{color:var(--muted);font-size:12px}
main{padding:20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(440px,1fr));gap:16px}.camera{min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:8px;overflow:hidden}
.camera-head{height:52px;padding:0 16px;display:flex;align-items:center;justify-content:space-between;gap:12px}.name{font-weight:650}.meta{color:var(--muted);font-size:12px;white-space:nowrap}.state{display:inline-flex;align-items:center;gap:7px}.dot{width:8px;height:8px;border-radius:50%;background:var(--red)}.online .dot{background:var(--green)}
.frame{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;background:#080a0b}.empty{grid-column:1/-1;border:1px dashed var(--line);border-radius:8px;padding:44px;text-align:center;color:var(--muted)}
@media(max-width:520px){header{padding:0 14px}.hint{display:none}main{padding:10px;grid-template-columns:1fr}.camera{border-radius:6px}}
</style></head><body>
<header><div class="brand"><b>VO</b>Vision Office Monitor</div><div class="hint">Локальный preview: RTSP подключён только к worker</div></header>
<main id="cameras"><div class="empty">Ожидаем кадры от камеры...</div></main>
<script>
const root=document.getElementById('cameras'); const cards=new Map();
function build(camera){const card=document.createElement('section');card.className='camera';card.id='camera-'+camera.camera_id;
card.innerHTML='<div class="camera-head"><div><div class="name"></div><div class="meta"></div></div><div class="state"><span class="dot"></span><span class="state-text"></span></div></div><img class="frame" alt="Кадр камеры">';return card}
function update(camera){let card=cards.get(camera.camera_id);if(!card){card=build(camera);cards.set(camera.camera_id,card);root.querySelector('.empty')?.remove();root.appendChild(card)}
card.querySelector('.name').textContent=camera.camera_name||camera.camera_id;card.querySelector('.meta').textContent='Захват '+(camera.capture_fps??0)+' FPS · Детекция '+(camera.detection_fps??0)+' FPS';
const online=camera.running&&camera.stream_status==='connected';card.querySelector('.state').classList.toggle('online',online);card.querySelector('.state-text').textContent=online?'В сети':'Переподключение';
const image=card.querySelector('img');image.src='/api/v1/cameras/'+encodeURIComponent(camera.camera_id)+'/preview.jpg?t='+Date.now()}
async function refresh(){try{const response=await fetch('/api/v1/monitor/status',{cache:'no-store'});if(!response.ok)throw new Error();const data=await response.json();const ids=new Set((data.cameras||[]).map(camera=>camera.camera_id));for(const [id,card] of cards){if(!ids.has(id)){card.remove();cards.delete(id)}}
if(!ids.size&&!root.querySelector('.empty'))root.innerHTML='<div class="empty">Нет активных камер или их статус ещё не опубликован.</div>';(data.cameras||[]).forEach(update)}catch(_error){}finally{setTimeout(refresh,250)}}refresh();
</script></body></html>"""


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
