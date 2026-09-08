import base64
import html
import os
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from database.manager import get_engine
from database.migrations import run_migrations
from database.models import AccessLogOutbox, Attendance, Base, EdgeSyncState, Employee, HealthIncident, RecognitionEvent, RemotePerson, RemotePersonReferencePhoto, UnknownFaceObservation, UnknownVisitor, UnknownVisitorVisit
from core.edge.config import load_edge_settings
from core.local_time import as_utc, format_local, local_day_bounds_utc, local_now, local_today, to_local
from core.performance import read_runtime_status
from core.preview import preview_path
from core.unknown_visitors import UnknownVisitorService


st.set_page_config(page_title="Vision Office", page_icon="VO", layout="wide", initial_sidebar_state="collapsed")


def apply_theme():
    css = (PROJECT_ROOT / "ui" / "theme.css").read_text(encoding="utf-8")
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


apply_theme()
engine = get_engine()
run_migrations(engine)
Session = sessionmaker(bind=engine)

for state_key in ("live_process", "demo_process", "api_process"):
    st.session_state.setdefault(state_key, None)


def managed_runtime() -> bool:
    """Docker runs worker/API separately, so the dashboard must not spawn duplicates."""
    return os.getenv("VISION_OFFICE_MANAGED_RUNTIME", "").strip().lower() in {"1", "true", "yes", "on"}


def api_public_url() -> str:
    return os.getenv("VISION_OFFICE_API_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")


@st.cache_resource
def get_recognizer():
    from core.ai.recognizer import FaceRecognizer

    return FaceRecognizer()


def create_local_employee(name, role, raw_photo):
    """Create a device-only person with an embedding stored in local PostgreSQL."""
    if not name or not name.strip():
        raise ValueError("Укажите полное имя.")
    if not role or not role.strip():
        raise ValueError("Укажите роль сотрудника.")
    image = cv2.imdecode(np.asarray(bytearray(raw_photo), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError("Фотография повреждена или имеет неподдерживаемый формат.")
    embedding = get_recognizer().get_embedding(image)
    if embedding is None:
        raise ValueError("На фотографии не найдено лицо.")
    faces_dir = PROJECT_ROOT / "data" / "faces"
    faces_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "_".join("".join(char for char in name.strip() if char.isalnum() or char in " _-").split()) or "local"
    photo_path = faces_dir / f"{safe_name}_{time.time_ns()}.jpg"
    if not cv2.imwrite(str(photo_path), image):
        raise ValueError("Не удалось сохранить локальную фотографию.")
    session = Session()
    try:
        employee = Employee(
            full_name=name.strip(),
            face_embeddings=[np.asarray(embedding, dtype=np.float32).tolist()],
            role=role.strip(),
            photo_path=str(photo_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        )
        session.add(employee)
        session.commit()
        session.refresh(employee)
        return employee
    except Exception:
        session.rollback()
        photo_path.unlink(missing_ok=True)
        raise
    finally:
        session.close()


def render_local_employee_form(form_key):
    st.subheader("Локальный сотрудник")
    st.caption("Профиль, фото и посещаемость останутся только в локальной PostgreSQL. События этого сотрудника не отправляются в ERP.")
    with st.form(form_key, clear_on_submit=True):
        left, right = st.columns([3, 2], gap="large")
        with left:
            name = st.text_input("Полное имя", placeholder="Например, Бекзод Хаитов")
            role = st.selectbox("Роль", ["Сотрудник", "Учитель", "Ученик", "Гость", "Другая"], key=f"{form_key}_role")
            custom_role = st.text_input("Название роли", key=f"{form_key}_custom_role") if role == "Другая" else ""
        with right:
            uploaded_file = st.file_uploader("Фотография", type=["jpg", "jpeg", "png"], key=f"{form_key}_photo")
        submitted = st.form_submit_button("Добавить локально", type="primary", icon=":material/person_add:", use_container_width=True)
    if not submitted:
        return
    if not uploaded_file:
        st.warning("Добавьте фотографию с хорошо видимым лицом.")
        return
    selected_role = custom_role.strip() if role == "Другая" else role
    try:
        with st.spinner("Проверяем лицо и создаём локальный биометрический шаблон"):
            employee = create_local_employee(name, selected_role, uploaded_file.getvalue())
        st.success(f"Локальный профиль #{employee.id:04d} создан. Камера начнёт использовать его в течение нескольких секунд.")
        st.rerun()
    except ValueError as error:
        st.error(str(error))
    except Exception:
        st.error("Не удалось создать локальный профиль. Проверьте логи и повторите попытку.")


def process_running(state_key):
    process = st.session_state.get(state_key)
    return process is not None and process.poll() is None


def start_process(state_key, script_name):
    if process_running(state_key):
        return
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    st.session_state[state_key] = subprocess.Popen([sys.executable, script_name], cwd=PROJECT_ROOT, creationflags=flags)


def stop_process(state_key):
    process = st.session_state.get(state_key)
    if process is None or process.poll() is not None:
        st.session_state[state_key] = None
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        process.wait(timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        process.terminate()
    st.session_state[state_key] = None


def start_api_process():
    if process_running("api_process"):
        return
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    st.session_state.api_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.server:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=PROJECT_ROOT,
        creationflags=flags,
    )


def photo_html(employee):
    path = PROJECT_ROOT / (employee.photo_path or "")
    if path.is_file():
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f'<img class="employee-photo" src="data:image/jpeg;base64,{encoded}" alt="">'
    initials = "".join(part[0] for part in employee.full_name.split()[:2]).upper()
    return f'<div class="employee-fallback">{html.escape(initials)}</div>'


def profile_photo_path(photo_path):
    """Resolve a database photo path without exposing external ERP URLs to the UI."""
    path = PROJECT_ROOT / (photo_path or "")
    return path if path.is_file() else None


def profile_photo_data_uri(photo_path):
    """Return a table-safe preview for a local image, never an ERP-hosted URL."""
    path = profile_photo_path(photo_path)
    if not path:
        return None
    mime_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def render_directory_table(rows, key):
    """Render a compact, paginated directory table without changing catalog data."""
    if not rows:
        st.caption("Поиск не нашёл сотрудников.")
        return []

    controls, page_size_control, page_control = st.columns([3, 1, 1], gap="small", vertical_alignment="bottom")
    with controls:
        st.markdown(
            f"<div class='directory-summary'>Найдено: {len(rows)}</div>",
            unsafe_allow_html=True,
        )
    with page_size_control:
        page_size = st.selectbox("На странице", [10, 25, 50], key=f"{key}_page_size")

    page_count = max(1, (len(rows) + page_size - 1) // page_size)
    page_key = f"{key}_page"
    if st.session_state.get(page_key, 1) > page_count:
        st.session_state[page_key] = 1
    with page_control:
        page = st.selectbox(
            "Страница",
            list(range(1, page_count + 1)),
            format_func=lambda value: f"{value} / {page_count}",
            key=page_key,
        )

    start = (page - 1) * page_size
    page_rows = rows[start:start + page_size]
    table_data = pd.DataFrame(page_rows)
    column_order = [
        column for column in ["№", "Фото", "Сотрудник", "Карточка", "Роль / тип", "Статус", "FaceID", "Доп. фото", "Посещений", "Наблюдения", "Первая встреча", "Последняя встреча", "Камера", "Профиль"]
        if column in table_data.columns
    ]
    st.dataframe(
        table_data,
        use_container_width=True,
        hide_index=True,
        column_order=column_order,
        height=min(52 + len(page_rows) * 48, 540),
        row_height=48,
        column_config={
            "№": st.column_config.NumberColumn("№", width="small", format="%d"),
            "Фото": st.column_config.ImageColumn("Фото", width="small"),
            "Сотрудник": st.column_config.TextColumn("Сотрудник", width="large"),
            "Роль / тип": st.column_config.TextColumn("Роль / тип", width="medium"),
            "FaceID": st.column_config.TextColumn("FaceID", width="medium"),
            "Доп. фото": st.column_config.NumberColumn("Доп. фото", width="small", format="%d"),
            "Посещений": st.column_config.NumberColumn("Посещений", width="medium", format="%d"),
            "Наблюдения": st.column_config.NumberColumn("Наблюдения", width="medium", format="%d"),
            "Статус": st.column_config.TextColumn("Статус", width="medium"),
            "Первая встреча": st.column_config.TextColumn("Первая встреча", width="medium"),
            "Последняя встреча": st.column_config.TextColumn("Последняя встреча", width="medium"),
            "Камера": st.column_config.TextColumn("Камера", width="medium"),
            "Профиль": st.column_config.TextColumn("Профиль", width="medium"),
        },
    )
    return page_rows


def render_profile_header(name, source, profile_id, photo_path, role, status, detail):
    """Shared, unframed profile heading for every employee catalog."""
    image = profile_photo_data_uri(photo_path)
    initials = "".join(part[0] for part in name.split()[:2]).upper() or "VO"
    portrait = (
        f'<img class="profile-portrait" src="{image}" alt="Фото профиля">'
        if image else f'<div class="employee-fallback">{html.escape(initials)}</div>'
    )
    tags = "".join(
        f'<span class="profile-tag">{html.escape(str(value))}</span>'
        for value in (role, status, detail)
    )
    st.markdown(
        f'<div class="profile-header">{portrait}<div class="profile-info">'
        f'<h2>{html.escape(display_name(name))}</h2>'
        f'<p>{html.escape(source)} · {html.escape(str(profile_id))}</p>'
        f'<div class="profile-tags">{tags}</div></div></div>',
        unsafe_allow_html=True,
    )


def render_event_history(events, empty_message):
    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
    st.subheader("Последняя активность")
    if not events:
        st.caption(empty_message)
        return
    st.dataframe(
        pd.DataFrame([{
            "Дата и время": format_local(event.created_at),
            "Камера": event.camera_id,
            "Событие": event.event_type,
            "Уверенность": f"{event.confidence:.0%}" if event.confidence is not None else "—",
        } for event in events]),
        use_container_width=True,
        hide_index=True,
    )


def render_local_attendance_history(attendance):
    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
    st.subheader("Локальная посещаемость")
    if not attendance:
        st.caption("Локальных записей посещаемости пока нет.")
        return
    st.dataframe(
        pd.DataFrame([{
            "Дата": format_local(item.timestamp, "%d.%m.%Y"),
            "Время": format_local(item.timestamp, "%H:%M:%S"),
            "Событие": item.event_type or "entry",
        } for item in attendance]),
        use_container_width=True,
        hide_index=True,
    )


def display_name(name):
    return name[:-4] if name.lower().endswith(".jpg") else name


def day_bounds(selected_date: date) -> tuple[datetime, datetime]:
    return local_day_bounds_utc(selected_date)


def status_badge(running, active_label, idle_label):
    return f'<span class="badge"><span class="dot {"dot-online" if running else "dot-idle"}"></span>{active_label if running else idle_label}</span>'


def render_header(title, subtitle):
    st.markdown(
        f'<div class="page-heading"><div><h1>{html.escape(title)}</h1>'
        f'<p>{html.escape(subtitle)}</p></div>'
        f'<span class="date-chip">{local_now().strftime("%d.%m.%Y")} · Asia/Tashkent</span></div>',
        unsafe_allow_html=True,
    )


def render_navigation():
    with st.container(key="navigation"):
        left, center, right = st.columns([1.55, 5.6, 0.45], gap="small", vertical_alignment="center")
        with left:
            st.markdown(
                '<div class="brand"><span class="brand-mark">VO</span><span class="brand-name">Vision Office</span></div>',
                unsafe_allow_html=True,
            )
        with center:
            page = st.radio(
                "Навигация",
                ["Панель", "Аналитика", "Сотрудники", "Регистрация", "API"],
                horizontal=True,
                label_visibility="collapsed",
            )
        with right:
            st.markdown('<div class="profile-dot">VO</div>', unsafe_allow_html=True)
    return page


def render_control_center():
    render_header("Операционный центр", "Обзор присутствия и состояния камер")
    edge_settings = load_edge_settings()
    runtime = read_runtime_status() or {}
    live_running = bool(runtime.get("running")) if managed_runtime() else process_running("live_process")
    demo_running = process_running("demo_process")
    session = Session()
    try:
        employee_count = (
            session.query(RemotePerson).filter(RemotePerson.active.is_(True)).count() + session.query(Employee).count()
            if edge_settings.configured else session.query(Employee).count()
        )
        day_start, day_end = day_bounds(local_today())
        today_count = session.query(Attendance.employee_id).filter(
            Attendance.timestamp >= day_start, Attendance.timestamp < day_end,
        ).distinct().count()
        event_times = session.query(RecognitionEvent.created_at).filter(
            RecognitionEvent.created_at >= day_start, RecognitionEvent.created_at < day_end,
        ).all()
        active_incidents = session.query(HealthIncident).filter(HealthIncident.status == "open").count()
    finally:
        session.close()

    cameras = runtime.get("cameras") or ([runtime] if runtime.get("camera_id") else [])
    connected = sum(camera.get("stream_status") == "connected" for camera in cameras)
    cards = [
        ("События сегодня", len(event_times), "Обнаружения на всех камерах", True),
        ("Сотрудники", employee_count, "Активные ERP и локальные профили", False),
        ("Сегодня замечены", today_count, "Локальная посещаемость", False),
        ("Камеры на связи", f"{connected} / {len(cameras)}", "Состояние видеопотоков", False),
    ]
    st.markdown('<div class="summary-grid">' + "".join(
        f'<div class="summary-card{" featured" if featured else ""}">'
        f'<div class="summary-top">{label}<span class="status-mark"></span></div>'
        f'<div class="summary-number">{value}</div><div class="summary-note">{note}</div></div>'
        for label, value, note, featured in cards
    ) + "</div>", unsafe_allow_html=True)
    st.markdown(
        f'<div class="status-strip">{status_badge(live_running, "Распознавание активно", "Распознавание остановлено")}'
        f'<span class="activity-meta">Обновлено в {local_now().strftime("%H:%M")}</span></div>',
        unsafe_allow_html=True,
    )

    chart_col, camera_col = st.columns([1.5, 1], gap="medium")
    with chart_col, st.container(key="activity-chart"):
        st.markdown('<p class="panel-title">Активность в течение дня</p><p class="panel-note">Обнаружения по часам</p>', unsafe_allow_html=True)
        hourly = [0] * 24
        for (timestamp,) in event_times:
            hourly[to_local(timestamp).hour] += 1
        current_hour = local_now().hour
        figure = go.Figure(go.Bar(
            x=list(range(24)), y=hourly,
            marker_color=["#108455" if hour == current_hour else "#b6d9c7" for hour in range(24)],
            hovertemplate="%{x}:00 · %{y} событий<extra></extra>",
        ))
        figure.update_layout(
            height=265, margin=dict(l=0, r=4, t=18, b=0),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(family="Arial, sans-serif", size=11, color="#737b76"),
            bargap=0.32, barcornerradius=12,
            xaxis=dict(tickmode="array", tickvals=list(range(0, 24, 3)),
                       ticktext=[f"{hour:02d}:00" for hour in range(0, 24, 3)], fixedrange=True, showgrid=False),
            yaxis=dict(rangemode="tozero", gridcolor="#edf0ed", griddash="dot", fixedrange=True),
        )
        st.plotly_chart(figure, use_container_width=True, config={"displayModeBar": False})

    with camera_col, st.container(key="camera-overview"):
        st.markdown('<p class="panel-title">Камера · обзор</p>', unsafe_allow_html=True)
        camera = next((item for item in cameras if item.get("stream_status") == "connected"), cameras[0] if cameras else {})
        path = preview_path(camera.get("camera_id", "camera"))
        shot = None
        try:
            if path.is_file():
                shot = profile_photo_data_uri(path)
        except OSError:
            pass
        if shot:
            st.markdown(
                f'<img class="camera-shot" src="{shot}" alt="Последний сохранённый кадр камеры">'
                f'<div class="camera-caption">{html.escape(str(camera.get("camera_name") or camera.get("camera_id") or "Камера"))}'
                f' · Снимок при открытии страницы</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown('<div class="empty-state">Кадр пока недоступен</div>', unsafe_allow_html=True)
        if managed_runtime():
            st.link_button("Открыть монитор", f"{api_public_url()}/monitor", icon=":material/open_in_new:", use_container_width=True)
        elif live_running:
            if st.button("Остановить камеру", key="stop_live", icon=":material/stop:", use_container_width=True):
                stop_process("live_process")
                st.rerun()
        elif st.button("Запустить камеру", key="start_live", icon=":material/play_arrow:", type="primary", use_container_width=True):
            start_process("live_process", "main.py")
            st.rerun()

    st.subheader("Последние обнаружения")
    recent_events = load_recent_events(limit=8)
    if recent_events.empty:
        st.markdown("<div class='empty-state'>Новых событий пока нет.</div>", unsafe_allow_html=True)
    else:
        st.dataframe(recent_events, use_container_width=True, hide_index=True)
    if active_incidents:
        st.warning(f"Мониторинг: открытых инцидентов — {active_incidents}", icon=":material/info:")
    with st.expander("Производительность и состояние камер", icon=":material/monitoring:"):
        render_performance_panel()
    with st.expander("Тестовый контур", icon=":material/science:"):
        if demo_running:
            st.info("Тест выполняется")
            if st.button("Остановить тест", key="stop_demo", icon=":material/stop:", use_container_width=True):
                stop_process("demo_process")
                st.rerun()
        elif st.button("Запустить тест", key="start_demo", icon=":material/play_arrow:", use_container_width=True):
            start_process("demo_process", "test_video.py")
            st.rerun()


def render_performance_panel():
    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
    st.subheader("Производительность edge-устройства")
    status = read_runtime_status()
    if not status:
        st.info("Показатели появятся после запуска камеры. Dashboard не обрабатывает видеокадры.")
        return
    if not status.get("running"):
        st.info("Камера остановлена. Последние показатели сброшены.")
        return
    cameras = status.get("cameras")
    if cameras:
        st.dataframe(
            pd.DataFrame([{
                "Камера": item.get("camera_name") or item.get("camera_id"),
                "Статус": item.get("stream_status", "—"),
                "Захват FPS": item.get("capture_fps", 0),
                "Детекция FPS": item.get("detection_fps", 0),
                "Возраст кадра, ms": item.get("frame_age_ms", "—"),
                "FaceID p95, ms": item.get("face_ms_p95", "—"),
                "Reconnect": item.get("reconnect_attempts", 0),
            } for item in cameras]),
            use_container_width=True,
            hide_index=True,
        )
        return
    metrics = st.columns(5)
    metrics[0].metric("YOLO", status.get("yolo_device", "—"))
    metrics[1].metric("Захват", f"{status.get('capture_fps', 0)} FPS")
    metrics[2].metric("Детекция", f"{status.get('detection_fps', 0)} FPS")
    metrics[3].metric("YOLO p95", f"{status.get('detection_ms_p95') or '—'} ms")
    metrics[4].metric("FaceID p95", f"{status.get('face_ms_p95') or '—'} ms")
    st.caption(
        f"Возраст кадра: {status.get('frame_age_ms') or '—'} ms · "
        f"FaceID задач: {status.get('face_tasks', 0)} · "
        f"Пропущено задач: {status.get('face_dropped', 0)} · "
        f"Очередь FaceID: {status.get('face_queue_size', '—')} · "
        f"ONNX: {', '.join(status.get('onnx_providers', []))}"
    )


def load_attendance(selected_date):
    day_start, day_end = day_bounds(selected_date)
    session = Session()
    try:
        rows = session.query(Attendance.timestamp, Attendance.event_type, Employee.full_name, Employee.role).join(Employee, Attendance.employee_id == Employee.id).filter(
            Attendance.timestamp >= day_start, Attendance.timestamp < day_end,
        ).order_by(Attendance.timestamp.asc()).all()
    finally:
        session.close()
    return pd.DataFrame([{
        "ФИО": row.full_name,
        "Роль": row.role or "Не указана",
        "Дата и время": to_local(row.timestamp).replace(tzinfo=None),
        "Событие": row.event_type or "check_in",
    } for row in rows])


def load_recent_events(limit):
    session = Session()
    try:
        rows = session.query(RecognitionEvent).order_by(RecognitionEvent.created_at.desc()).limit(limit).all()
    finally:
        session.close()
    return pd.DataFrame([{
        "ФИО": display_name(row.person_name or "Неизвестный"),
        "Роль": {"unknown": "Неизвестный", "local": "Локальный", "employee": "Сотрудник"}.get(row.person_type, row.person_type),
        "Время": format_local(row.created_at, "%d.%m %H:%M"),
        "Событие": {"entry": "Вход", "exit": "Выход"}.get(row.event_type, row.event_type),
        "Камера": row.camera_id,
    } for row in rows])


def render_analytics():
    render_header("Аналитика присутствия", "Сводка входов и дисциплины по выбранной дате")
    selected_date = st.date_input("Дата", value=local_today(), label_visibility="collapsed", width=240, format="DD.MM.YYYY")
    df = load_attendance(selected_date)
    if df.empty:
        st.markdown("<div class='empty-state'>За выбранную дату событий нет.</div>", unsafe_allow_html=True)
        return
    df["Дата и время"] = pd.to_datetime(df["Дата и время"])
    first_events = df.sort_values("Дата и время").drop_duplicates(subset=["ФИО"], keep="first").copy()
    work_start = pd.Timestamp(datetime.combine(selected_date, datetime.min.time())).replace(hour=9)
    first_events["Статус"] = np.where(first_events["Дата и время"] <= work_start, "Вовремя", "Опоздание")
    metrics = st.columns(3)
    metrics[0].metric("Присутствуют", len(first_events))
    metrics[1].metric("Вовремя", int((first_events["Статус"] == "Вовремя").sum()))
    metrics[2].metric("Опоздания", int((first_events["Статус"] == "Опоздание").sum()))
    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)

    chart_col, status_col = st.columns([2, 1], gap="large")
    hourly = first_events.assign(Час=first_events["Дата и время"].dt.hour).groupby("Час").size().reset_index(name="Количество")
    with chart_col, st.container(key="analytics-arrivals"):
        st.subheader("Приходы по часам")
        arrivals = go.Figure(go.Bar(x=hourly["Час"], y=hourly["Количество"], width=0.65, marker_color="#108455", hovertemplate="%{x}:00 · %{y} сотрудника<extra></extra>"))
        arrivals.update_layout(height=280, barcornerradius=10, margin=dict(l=0, r=0, t=12, b=0), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(family="Segoe UI, Arial, sans-serif", size=12, color="#737b76"), xaxis=dict(title=None, range=[-0.5, 23.5], tickmode="linear", dtick=3, gridcolor="#edf0ed"), yaxis=dict(title=None, rangemode="tozero", dtick=1 if hourly["Количество"].max() < 5 else None, gridcolor="#edf0ed"), showlegend=False)
        st.plotly_chart(arrivals, use_container_width=True, config={"displayModeBar": False})
    with status_col, st.container(key="analytics-discipline"):
        st.subheader("Дисциплина")
        status_counts = first_events["Статус"].value_counts()
        discipline = go.Figure(go.Pie(labels=status_counts.index, values=status_counts.values, hole=.78, marker_colors=["#108455" if item == "Вовремя" else "#b44e58" for item in status_counts.index], textinfo="none"))
        discipline.update_layout(height=280, margin=dict(l=8, r=8, t=12, b=0), font=dict(family="Segoe UI, Arial, sans-serif", size=12, color="#737b76"), legend=dict(orientation="h", x=0.5, xanchor="center", y=-0.05), paper_bgcolor="rgba(0,0,0,0)", showlegend=True)
        st.plotly_chart(discipline, use_container_width=True, config={"displayModeBar": False})
    st.subheader("Первое появление")
    first_events["Время"] = first_events["Дата и время"].dt.strftime("%H:%M")
    st.dataframe(first_events[["ФИО", "Роль", "Время", "Статус"]], use_container_width=True, hide_index=True, column_config={"Статус": st.column_config.TextColumn(width="small")})


def unknown_state_label(state):
    return {"active": "Требует проверки", "converted": "Зарегистрирован", "archived": "Архив"}.get(state, state)


def render_unknown_visitor_catalog():
    """Render local candidate visitors without touching ERP or camera processing."""
    session = Session()
    try:
        visitors = session.query(UnknownVisitor).order_by(UnknownVisitor.last_seen_at.desc()).all()
        latest_camera = {}
        for visit in session.query(UnknownVisitorVisit).order_by(UnknownVisitorVisit.last_seen_at.desc()).all():
            latest_camera.setdefault(visit.visitor_id, visit.camera_id)
    finally:
        session.close()

    active_count = sum(item.state == "active" for item in visitors)
    converted_count = sum(item.state == "converted" for item in visitors)
    first, second, third = st.columns(3)
    first.metric("Требуют проверки", active_count)
    second.metric("Зарегистрированы", converted_count)
    third.metric("Карточек", len(visitors))
    st.caption("Группы формируются локально по высокой похожести лица. Фото и биометрические шаблоны не передаются в ERP.")
    if not visitors:
        st.info("Каталог заполняется в фоне из новых и сохранённых неизвестных событий.")
        return

    controls, filter_col = st.columns([3, 1], gap="small", vertical_alignment="bottom")
    with controls:
        search = st.text_input("Поиск неизвестных", placeholder="Номер карточки", key="unknown_people_search")
    with filter_col:
        state_filter = st.selectbox("Статус", ["Все", "Требует проверки", "Зарегистрирован", "Архив"], key="unknown_people_state")
    search_value = search.strip().casefold()
    state_values = {"Требует проверки": "active", "Зарегистрирован": "converted", "Архив": "archived"}
    filtered = [
        visitor for visitor in visitors
        if (not search_value or search_value in f"{visitor.id:04d}" or search_value in f"unknown {visitor.id}".casefold())
        and (state_filter == "Все" or visitor.state == state_values[state_filter])
    ]
    page_rows = render_directory_table([
        {
            "№": index,
            "Фото": profile_photo_data_uri(visitor.primary_photo_path),
            "Карточка": f"Неизвестный #{visitor.id:04d}",
            "Статус": unknown_state_label(visitor.state),
            "Посещений": visitor.visit_count,
            "Наблюдения": visitor.observation_count,
            "Первая встреча": format_local(visitor.first_seen_at, "%d.%m.%Y %H:%M"),
            "Последняя встреча": format_local(visitor.last_seen_at, "%d.%m.%Y %H:%M"),
            "Камера": latest_camera.get(visitor.id, "—"),
            "Профиль": "Открыть ниже",
        } for index, visitor in enumerate(filtered, start=1)
    ], "unknown_visitors")
    if not page_rows:
        return
    options = {
        f"{row['Карточка']} · {row['Статус']}": filtered[row["№"] - 1]
        for row in page_rows
    }
    selected_label = st.selectbox("Открыть карточку неизвестного", list(options), key="unknown_visitor_detail")
    visitor = options[selected_label]
    session = Session()
    try:
        observations = session.query(UnknownFaceObservation, RecognitionEvent).join(
            RecognitionEvent, RecognitionEvent.id == UnknownFaceObservation.event_id
        ).filter(
            UnknownFaceObservation.visitor_id == visitor.id,
        ).order_by(UnknownFaceObservation.observed_at.desc()).all()
        linked_employee = session.get(Employee, visitor.local_employee_id) if visitor.local_employee_id else None
        active_candidates = session.query(UnknownVisitor).filter(
            UnknownVisitor.state == "active", UnknownVisitor.id != visitor.id,
        ).order_by(UnknownVisitor.last_seen_at.desc()).all()
    finally:
        session.close()
    render_profile_header(
        f"Неизвестный #{visitor.id:04d}",
        "Локальный каталог",
        f"Карточка #{visitor.id:04d}",
        visitor.primary_photo_path,
        "Неизвестный посетитель",
        unknown_state_label(visitor.state),
        f"{visitor.visit_count} посещ. · {visitor.observation_count} наблюд.",
    )
    metrics = st.columns(4)
    metrics[0].metric("Посещения", visitor.visit_count)
    metrics[1].metric("Наблюдения", visitor.observation_count)
    metrics[2].metric("Первая встреча", format_local(visitor.first_seen_at, "%d.%m %H:%M"))
    metrics[3].metric("Последняя встреча", format_local(visitor.last_seen_at, "%d.%m %H:%M"))
    if linked_employee:
        st.success(f"Карточка подтверждена как локальный сотрудник: {display_name(linked_employee.full_name)} · #{linked_employee.id:04d}. История неизвестного сохранена отдельно.")

    event_rows = [event for _observation, event in observations]
    image_events = [(observation, event) for observation, event in observations if profile_photo_path(event.photo_path)]
    if image_events:
        st.subheader("Последние фотографии")
        columns = st.columns(min(4, len(image_events)))
        for column, (_observation, event) in zip(columns * ((len(image_events) + len(columns) - 1) // len(columns)), image_events[:8]):
            with column:
                st.image(str(profile_photo_path(event.photo_path)), caption=format_local(event.created_at, "%d.%m %H:%M"), use_container_width=True)
    render_event_history(event_rows[:20], "Для этой карточки пока нет доступных событий.")

    if visitor.state == "active":
        usable = [(observation, event) for observation, event in observations if observation.processing_status == "clustered" and profile_photo_path(event.photo_path)]
        if usable:
            st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
            st.subheader("Зарегистрировать как локального сотрудника")
            st.caption("Выбранное фото будет скопировано в локальный профиль. Прошлые неизвестные события не меняются и не отправляются в ERP.")
            choices = {f"{format_local(event.created_at, '%d.%m.%Y %H:%M:%S')} · {event.camera_id}": observation.id for observation, event in usable}
            with st.form(f"unknown_convert_{visitor.id}", clear_on_submit=True):
                left, right = st.columns(2)
                with left:
                    full_name = st.text_input("Полное имя", key=f"unknown_name_{visitor.id}")
                    role = st.text_input("Роль", value="Сотрудник", key=f"unknown_role_{visitor.id}")
                with right:
                    primary_label = st.selectbox("Основная фотография", list(choices), key=f"unknown_primary_{visitor.id}")
                    additional_labels = st.multiselect("Дополнительные шаблоны", list(choices), key=f"unknown_extra_{visitor.id}")
                submitted = st.form_submit_button("Создать локального сотрудника", use_container_width=True)
            if submitted:
                try:
                    employee = UnknownVisitorService().convert_to_local_employee(
                        visitor.id, full_name, role, choices[primary_label], [choices[label] for label in additional_labels],
                    )
                    st.success(f"Создан локальный профиль #{employee.id:04d}. Камера начнёт использовать его в течение нескольких секунд.")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
                except Exception:
                    st.error("Не удалось создать локальный профиль. Проверьте логи и повторите.")

        with st.expander("Исправить группировку"):
            st.caption("Разделяйте только ошибочно объединённые наблюдения. Это не изменяет исходные журнальные события.")
            split_choices = {
                f"{format_local(event.created_at, '%d.%m.%Y %H:%M:%S')} · {event.camera_id}": observation.id
                for observation, event in observations if observation.processing_status == "clustered"
            }
            selected_for_split = st.multiselect("Наблюдения для новой карточки", list(split_choices), key=f"unknown_split_{visitor.id}")
            if st.button("Разделить выбранные", key=f"unknown_split_action_{visitor.id}", type="secondary"):
                try:
                    created = UnknownVisitorService().split(visitor.id, [split_choices[label] for label in selected_for_split])
                    st.success(f"Создана карточка неизвестного #{created.id:04d}.")
                    st.rerun()
                except ValueError as error:
                    st.warning(str(error))
            if active_candidates:
                merge_options = {f"Неизвестный #{candidate.id:04d} · {candidate.visit_count} посещ.": candidate.id for candidate in active_candidates}
                target_label = st.selectbox("Объединить с карточкой", list(merge_options), key=f"unknown_merge_{visitor.id}")
                if st.button("Объединить карточки", key=f"unknown_merge_action_{visitor.id}", type="secondary"):
                    try:
                        target = UnknownVisitorService().merge(visitor.id, merge_options[target_label])
                        st.success(f"Наблюдения объединены в карточку #{target.id:04d}.")
                        st.rerun()
                    except ValueError as error:
                        st.warning(str(error))


def render_people():
    render_header("Сотрудники", "Профили и последние события присутствия")
    edge_settings = load_edge_settings()
    if edge_settings.configured:
        session = Session()
        try:
            people = session.query(RemotePerson).filter(
                RemotePerson.active.is_(True)
            ).order_by(RemotePerson.fio.asc(), RemotePerson.id.asc()).all()
            photo_counts = dict(session.query(
                RemotePersonReferencePhoto.person_id,
                func.count(RemotePersonReferencePhoto.id),
            ).filter(
                RemotePersonReferencePhoto.active.is_(True)
            ).group_by(RemotePersonReferencePhoto.person_id).all())
            local_employees = session.query(Employee).order_by(Employee.full_name.asc(), Employee.id.asc()).all()
            local_event_counts = dict(session.query(
                Attendance.employee_id,
                func.count(Attendance.id),
            ).group_by(Attendance.employee_id).all())
            unknown_visitor_count = session.query(UnknownVisitor).filter(UnknownVisitor.state == "active").count()
        finally:
            session.close()
        status_names = {
            "ready": "Готов",
            "pending": "Обрабатывается",
            "invalid": "Требуется фото",
        }
        ready_count = sum(person.embedding_status == "ready" for person in people)
        first, second, third, fourth = st.columns(4)
        first.metric("Сотрудники ERP", len(people))
        second.metric("Локальные сотрудники", len(local_employees))
        third.metric("ERP готовы к распознаванию", ready_count)
        fourth.metric("Неизвестные", unknown_visitor_count)
        erp_tab, local_tab, unknown_tab = st.tabs([f"ERP · {len(people)}", f"Локальная база · {len(local_employees)}", f"Неизвестные · {unknown_visitor_count}"])
        with erp_tab:
            if not people:
                st.info("Каталог ERP ещё не загружен в локальный кэш.")
            else:
                st.caption("Основной каталог поступает из ERP. Локальные дополнительные фото не изменяют карточки ERP.")
                search = st.text_input("Поиск в ERP", placeholder="Имя или тип", key="erp_people_search")
                search_value = search.strip().casefold()
                filtered_people = [person for person in people if not search_value or search_value in (person.fio or "").casefold() or search_value in person.person_type.casefold()]
                page_people = render_directory_table(
                    [{
                        "№": index,
                        "Фото": profile_photo_data_uri(person.photo_path),
                        "Сотрудник": display_name(person.fio or f"{person.person_type} {person.id[:8]}"),
                        "Роль / тип": person.person_type,
                        "FaceID": status_names.get(person.embedding_status, person.embedding_status),
                        "Доп. фото": photo_counts.get(person.id, 0),
                        "Профиль": "Открыть ниже",
                    } for index, person in enumerate(filtered_people, start=1)],
                    "erp_people",
                )
                if page_people:
                    options = {
                        f"{row['Сотрудник']} · {filtered_people[row['№'] - 1].id[:8]}": filtered_people[row["№"] - 1]
                        for row in page_people
                    }
                    selected_label = st.selectbox("Открыть профиль ERP", list(options), key="erp_people_detail")
                    person = options[selected_label]
                    session = Session()
                    try:
                        events = session.query(RecognitionEvent).filter(
                            RecognitionEvent.person_id == person.id
                        ).order_by(RecognitionEvent.created_at.desc()).limit(12).all()
                    finally:
                        session.close()
                    render_profile_header(
                        person.fio or f"{person.person_type} {person.id[:8]}",
                        "ERP-каталог",
                        f"ERP {person.id[:8]}",
                        person.photo_path,
                        person.person_type,
                        status_names.get(person.embedding_status, person.embedding_status),
                        f"основное + {photo_counts.get(person.id, 0)} доп.",
                    )
                    render_event_history(events, "У этого ERP-сотрудника пока нет локальных событий распознавания.")
        with local_tab:
            if not local_employees:
                st.caption("Локальных сотрудников пока нет. Их можно добавить на вкладке «Регистрация».")
            else:
                st.caption("Эти профили и их посещаемость остаются на устройстве и не передаются в ERP.")
                search = st.text_input("Поиск в локальной базе", placeholder="Имя или роль", key="local_people_search")
                search_value = search.strip().casefold()
                filtered_employees = [employee for employee in local_employees if not search_value or search_value in employee.full_name.casefold() or search_value in (employee.role or "").casefold()]
                page_employees = render_directory_table(
                    [{
                        "№": index,
                        "Фото": profile_photo_data_uri(employee.photo_path),
                        "Сотрудник": display_name(employee.full_name),
                        "Роль / тип": employee.role or "Не указана",
                        "FaceID": "Готов" if employee.face_embeddings else "Нет",
                        "Посещений": local_event_counts.get(employee.id, 0),
                        "Профиль": "Открыть ниже",
                    } for index, employee in enumerate(filtered_employees, start=1)],
                    "local_people",
                )
                if page_employees:
                    options = {
                        f"{row['Сотрудник']} · #{filtered_employees[row['№'] - 1].id:04d}": filtered_employees[row["№"] - 1]
                        for row in page_employees
                    }
                    selected_label = st.selectbox("Открыть локальный профиль", list(options), key="local_people_detail")
                    employee = options[selected_label]
                    session = Session()
                    try:
                        attendance = session.query(Attendance).filter(
                            Attendance.employee_id == employee.id
                        ).order_by(Attendance.timestamp.desc()).limit(12).all()
                        events = session.query(RecognitionEvent).filter(
                            RecognitionEvent.person_id == f"local:{employee.id}"
                        ).order_by(RecognitionEvent.created_at.desc()).limit(12).all()
                    finally:
                        session.close()
                    render_profile_header(
                        employee.full_name,
                        "Локальная PostgreSQL",
                        f"Профиль #{employee.id:04d}",
                        employee.photo_path,
                        employee.role or "Не указана",
                        "Готов" if employee.face_embeddings else "Нет",
                        "локальное",
                    )
                    render_local_attendance_history(attendance)
                    render_event_history(events, "Событий камеры для этого локального профиля пока нет.")
        with unknown_tab:
            render_unknown_visitor_catalog()
        return
    session = Session()
    try:
        employees = session.query(Employee).order_by(Employee.full_name.asc()).all()
        if not employees:
            st.markdown("<div class='empty-state'>В базе пока нет сотрудников.</div>", unsafe_allow_html=True)
            return
        options = {f"{display_name(employee.full_name)}  |  #{employee.id:04d}": employee for employee in employees}
        selected_label = st.selectbox("Сотрудник", list(options), label_visibility="collapsed")
        employee = options[selected_label]
        logs = session.query(Attendance).filter(Attendance.employee_id == employee.id).order_by(Attendance.timestamp.desc()).limit(20).all()
    finally:
        session.close()
    st.markdown(f'''<div class="employee-head">{photo_html(employee)}<div><div class="employee-name">{html.escape(display_name(employee.full_name))}</div><div class="employee-role">{html.escape(employee.role or "Не указана")}</div><div class="employee-meta">Профиль #{employee.id:04d} &nbsp;&middot;&nbsp; Событий: {len(logs)}</div></div></div>''', unsafe_allow_html=True)
    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
    st.subheader("Последние события")
    if logs:
        st.dataframe(pd.DataFrame([{"Дата": format_local(log.timestamp, "%d.%m.%Y"), "Время": format_local(log.timestamp, "%H:%M:%S"), "Событие": log.event_type} for log in logs]), use_container_width=True, hide_index=True)
    else:
        st.markdown("<div class='empty-state'>Событий для этого профиля пока нет.</div>", unsafe_allow_html=True)

    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
    st.subheader("Состав базы")
    st.dataframe(
        pd.DataFrame([{"ID": f"#{item.id:04d}", "ФИО": display_name(item.full_name), "Роль": item.role or "Не указана"} for item in employees]),
        use_container_width=True,
        hide_index=True,
    )


def render_registration():
    edge_settings = load_edge_settings()
    if edge_settings.enabled:
        render_edge_status(edge_settings)
        return
    render_header("Регистрация сотрудника", "Создание профиля и биометрического шаблона")
    render_local_employee_form("registration_form")


def render_edge_status(edge_settings):
    render_header("Синхронизация людей", "ERP обновляет основной каталог каждый час; локальные фото расширяют только распознавание")
    session = Session()
    try:
        active_people = session.query(RemotePerson).filter(RemotePerson.active.is_(True)).count()
        inactive_people = session.query(RemotePerson).filter(RemotePerson.active.is_(False)).count()
        embeddings = session.query(RemotePerson).filter(
            RemotePerson.active.is_(True), RemotePerson.embedding.is_not(None)
        ).count()
        queued = session.query(AccessLogOutbox).filter(AccessLogOutbox.status.in_(["pending", "retry"])).count()
        failed = session.query(AccessLogOutbox).filter(AccessLogOutbox.status == "failed").count()
        sync_state = session.get(EdgeSyncState, 1)
        local_photo_count = session.query(RemotePersonReferencePhoto).filter(RemotePersonReferencePhoto.active.is_(True)).count()
        people = session.query(RemotePerson).filter(RemotePerson.active.is_(True)).order_by(RemotePerson.fio.asc(), RemotePerson.id.asc()).all()
        photo_counts = dict(session.query(
            RemotePersonReferencePhoto.person_id,
            func.count(RemotePersonReferencePhoto.id),
        ).filter(RemotePersonReferencePhoto.active.is_(True)).group_by(RemotePersonReferencePhoto.person_id).all())
    finally:
        session.close()

    if not edge_settings.configured:
        st.error("Интеграция включена, но не заполнены base_url, device_api_key или device_id в локальном settings.yaml.")
        st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
        render_local_employee_form("edge_local_employee_form")
        return
    metrics = st.columns(6)
    metrics[0].metric("Активные люди", active_people)
    metrics[1].metric("Неактивные", inactive_people)
    metrics[2].metric("С embeddings", embeddings)
    metrics[3].metric("Доп. фото", local_photo_count)
    metrics[4].metric("В очереди", queued)
    metrics[5].metric("Требуют внимания", failed)
    if sync_state and sync_state.last_error:
        st.warning(f"Последняя ошибка синхронизации: {sync_state.last_error}")
    elif sync_state and sync_state.last_incremental_sync_at:
        st.success(f"Последняя синхронизация: {format_local(sync_state.last_incremental_sync_at)}")
    else:
        st.info("Синхронизация начнётся при запуске edge-sync.")

    sync_action, sync_request_status = st.columns([1, 2], gap="large", vertical_alignment="bottom")
    with sync_action:
        if st.button("Обновить из ERP", key="request_erp_full_sync", icon=":material/sync:", use_container_width=True):
            from core.edge.service import EdgeService

            try:
                requested_at = EdgeService(edge_settings).request_full_sync()
                st.success(f"Запрос принят: {format_local(requested_at)}")
            except Exception:
                st.error("Не удалось поставить обновление в очередь. Проверьте статус PostgreSQL и повторите.")
    with sync_request_status:
        request_at = sync_state.manual_full_sync_requested_at if sync_state else None
        completed_at = sync_state.last_manual_full_sync_at if sync_state else None
        if request_at and (completed_at is None or as_utc(request_at) > as_utc(completed_at)):
            st.info(f"Полное обновление ERP ожидает edge-sync: {format_local(request_at)}")
        elif completed_at:
            st.caption(f"Последнее ручное полное обновление: {format_local(completed_at)}")

    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
    render_local_employee_form("edge_local_employee_form")

    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
    st.subheader("Дополнительные фото для распознавания")
    st.caption(
        f"Фото сохраняются только на этом устройстве, не отправляются в ERP и добавляют отдельный embedding. Лимит: {edge_settings.local_reference_photo_limit} на человека."
    )
    if not people:
        st.info("Сначала дождитесь первой синхронизации с ERP: список людей пока пуст.")
        return

    options = {
        f"{display_name(person.fio or 'Без имени')} · {person.id[:8]}": person.id
        for person in people
    }
    with st.form("edge_reference_photo_form", clear_on_submit=True):
        selected_label = st.selectbox("Сотрудник из ERP", list(options))
        uploaded_photo = st.file_uploader("Дополнительная фотография", type=["jpg", "jpeg", "png"])
        submitted = st.form_submit_button("Добавить фото", use_container_width=True)
    if submitted:
        if uploaded_photo is None:
            st.warning("Выберите фотографию с хорошо видимым лицом.")
        else:
            from core.edge.service import EdgeService

            try:
                with st.spinner("Проверяем лицо и создаём локальный биометрический шаблон"):
                    record = EdgeService(edge_settings).add_local_reference_photo(
                        options[selected_label], uploaded_photo.getvalue()
                    )
                st.success(f"Фото добавлено. Локальный шаблон {record.id[:8]} начнёт использоваться камерой в течение нескольких секунд.")
                st.rerun()
            except ValueError as error:
                st.error(str(error))
            except Exception:
                st.error("Не удалось добавить фото. Проверьте логи и повторите попытку.")

    enriched = [
        {
            "Сотрудник": display_name(person.fio or "Без имени"),
            "ERP ID": person.id[:8],
            "Локальных фото": photo_counts.get(person.id, 0),
        }
        for person in people
        if photo_counts.get(person.id, 0)
    ]
    if enriched:
        st.dataframe(pd.DataFrame(enriched), use_container_width=True, hide_index=True)


def render_developer_api():
    render_header("API для разработчиков", "Read-only интеграция с сотрудниками и событиями присутствия")
    api_running = managed_runtime() or process_running("api_process")
    st.markdown(
        f'''<div class="status-strip"><div><div class="status-label">Integration API</div><div class="status-value">{"Готов к запросам" if api_running else "Остановлен"}</div></div><div>{status_badge(api_running, "localhost:8000", "Локальный режим")}</div><div class="activity-meta">Доступ: только чтение<br>Версия: v1</div></div>''',
        unsafe_allow_html=True,
    )

    actions, endpoint_info = st.columns([1, 2], gap="large")
    with actions:
        with st.container(key="api-runtime"):
            st.markdown("<p class='panel-title'>Локальный сервер</p><p class='panel-note'>127.0.0.1:8000</p><br>", unsafe_allow_html=True)
            if managed_runtime():
                st.success("API запущен отдельным контейнером")
                st.link_button("Открыть документацию", f"{api_public_url()}/docs", use_container_width=True)
            elif api_running:
                if st.button("Остановить API", key="stop_api", type="secondary", use_container_width=True):
                    stop_process("api_process")
                    st.rerun()
                st.link_button("Открыть документацию", "http://127.0.0.1:8000/docs", use_container_width=True)
            elif st.button("Запустить API", key="start_api", type="primary", use_container_width=True):
                start_api_process()
                st.rerun()
    with endpoint_info:
        st.subheader("Доступные маршруты")
        st.dataframe(
            pd.DataFrame(
                [
                    {"Метод": "GET", "Маршрут": "/api/v1/health", "Назначение": "Проверка сервиса"},
                    {"Метод": "GET", "Маршрут": "/api/v1/employees", "Назначение": "Список сотрудников"},
                    {"Метод": "GET", "Маршрут": "/api/v1/attendance?date=YYYY-MM-DD", "Назначение": "События присутствия"},
                    {"Метод": "GET", "Маршрут": "/api/v1/attendance/summary?date=YYYY-MM-DD", "Назначение": "Сводка по дате"},
                    {"Метод": "GET", "Маршрут": "/api/v1/recognition-events", "Назначение": "События камер с фото"},
                    {"Метод": "GET", "Маршрут": "/api/v1/unknown-visitors", "Назначение": "Локальный каталог неизвестных"},
                    {"Метод": "GET", "Маршрут": "/api/v1/unknown-visitors/{id}", "Назначение": "Карточка неизвестного и история"},
                    {"Метод": "GET", "Маршрут": "/api/v1/status", "Назначение": "Камеры и Health Checker"},
                    {"Метод": "GET", "Маршрут": "/api/v1/incidents", "Назначение": "История инцидентов"},
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
    st.subheader("Пример запроса")
    st.code('curl "http://127.0.0.1:8000/api/v1/attendance?date=2026-08-25"', language="powershell")
    st.caption("Для защищённого доступа задайте VISION_OFFICE_API_KEY перед запуском и передавайте его в заголовке X-API-Key.")


page = render_navigation()

if page == "Панель":
    render_control_center()
elif page == "Аналитика":
    render_analytics()
elif page == "Сотрудники":
    render_people()
elif page == "Регистрация":
    render_registration()
else:
    render_developer_api()
