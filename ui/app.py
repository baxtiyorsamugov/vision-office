import base64
import html
import os
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import func, or_
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

from database.manager import get_engine
from database.migrations import run_migrations
from database.models import AccessLogOutbox, Attendance, Base, EdgeSyncState, EduSchoolCatalogPerson, EduSchoolCatalogSyncState, EduSchoolDeliveryAttempt, EduSchoolReferencePhoto, EduSchoolTurnstileOutbox, EduSchoolTurnstileState, Employee, HealthIncident, RecognitionEvent, RemotePerson, RemotePersonReferencePhoto, UnknownFaceObservation, UnknownVisitor, UnknownVisitorVisit
from core.edge.config import load_edge_settings
from core.local_time import as_utc, format_local, local_day_bounds_utc, local_now, local_today, to_local
from core.performance import read_runtime_status, configured_camera_statuses
from core.preview import preview_path
from core.unknown_visitors import UnknownVisitorService


st.set_page_config(page_title="Vision Office", page_icon="VO", layout="wide", initial_sidebar_state="collapsed")


def apply_theme():
    css = (PROJECT_ROOT / "ui" / "theme.css").read_text(encoding="utf-8")
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


apply_theme()
@st.cache_resource
def ui_engine():
    value = get_engine()
    run_migrations(value)
    return value


engine = ui_engine()
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


def render_person_picker(items, key, identity, name, status):
    selected_key = f"{key}_selected_id"
    selected_id = st.session_state.get(selected_key)
    selected = next((item for item in items if identity(item) == selected_id), items[0])
    st.session_state[selected_key] = identity(selected)
    with st.container(height=min(540, len(items) * 48 + 8), border=False, key=f"{key}_rows"):
        for item in items:
            name_col, status_col = st.columns([3, 1], gap="small", vertical_alignment="center")
            with name_col:
                if st.button(
                    display_name(name(item)), key=f"{key}_open_{identity(item)}",
                    type="primary" if identity(item) == identity(selected) else "secondary",
                    use_container_width=True,
                ):
                    st.session_state[selected_key] = identity(item)
                    st.rerun()
            with status_col:
                st.markdown(f"<span class='directory-face'>{html.escape(str(status(item)))}</span>",
                            unsafe_allow_html=True)
    return selected


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
            "Событие": {"entry": "Вход", "exit": "Выход"}.get(event.event_type, event.event_type),
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
            "Событие": {"entry": "Вход", "exit": "Выход"}.get(item.event_type or "entry", item.event_type or "entry"),
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
                ["Панель", "Камеры", "Аналитика", "Отчёты", "Сотрудники", "Регистрация", "Отправки", "API"],
                horizontal=True,
                label_visibility="collapsed",
            )
        with right:
            st.markdown('<div class="profile-dot">VO</div>', unsafe_allow_html=True)
    return page


def render_control_center():
    from core.analytics import utc_hour_expression
    render_header("Операционный центр", "Обзор присутствия и состояния камер")
    edge_settings = load_edge_settings()
    cameras = configured_camera_statuses()
    live_running = any(item.get("running") and item.get("ai_ready") for item in cameras)
    demo_running = process_running("demo_process")
    session = Session()
    try:
        employee_count = session.query(EduSchoolCatalogPerson).filter_by(active=True, person_type="employee").count() + session.query(Employee).count()
        day_start, day_end = map(as_utc, day_bounds(local_today()))
        today_count = session.query(RecognitionEvent.person_type, RecognitionEvent.person_id).filter(
            RecognitionEvent.created_at >= day_start, RecognitionEvent.created_at < day_end,
            RecognitionEvent.person_type.in_(("eduschool_employee", "eduschool_student", "local_employee")),
            RecognitionEvent.person_id.isnot(None),
        ).distinct().count()
        hour_expression = utc_hour_expression(engine, RecognitionEvent.created_at)
        event_hours = session.query(hour_expression.label("hour"), func.count()).filter(
            RecognitionEvent.created_at >= day_start, RecognitionEvent.created_at < day_end,
        ).group_by(hour_expression).all()
        active_incidents = session.query(HealthIncident).filter(HealthIncident.status == "open").count()
        delivery_attention = session.query(EduSchoolTurnstileOutbox).filter(
            or_(EduSchoolTurnstileOutbox.status.in_(("retry", "ambiguous")),
                (EduSchoolTurnstileOutbox.status == "blocked") & (EduSchoolTurnstileOutbox.attempts > 0))
        ).count()
    finally:
        session.close()

    active_cameras = [camera for camera in cameras if camera.get("enabled")]
    connected = sum(camera.get("running") and camera.get("stream_status") == "connected" for camera in active_cameras)
    cards = [
        ("События сегодня", sum(count for _, count in event_hours), "Обнаружения на всех камерах", True),
        ("Сотрудники", employee_count, "EduSchool и локальные профили", False),
        ("Сегодня замечены", today_count, "Сотрудники и ученики", False),
        ("Камеры на связи", f"{connected} / {len(active_cameras)}", "Активные камеры", False),
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
        for utc_hour, count in event_hours:
            timestamp = datetime.combine(local_today(), datetime.min.time()).replace(hour=int(utc_hour), tzinfo=timezone.utc)
            hourly[to_local(timestamp).hour] += count
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
        shot_time = ""
        try:
            if path.is_file():
                shot = profile_photo_data_uri(path)
                shot_time = format_local(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
        except OSError:
            pass
        if shot:
            st.markdown(
                f'<img class="camera-shot" src="{shot}" alt="Последний сохранённый кадр камеры">'
                f'<div class="camera-caption">{html.escape(str(camera.get("camera_name") or camera.get("camera_id") or "Камера"))}'
                f' · Кадр от {html.escape(shot_time)}</div>',
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
    if delivery_attention:
        st.warning(f"Отправка EduSchool: {delivery_attention} событий требуют внимания. Подробности во вкладке «Отправки».")
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
    from ui.cameras import render_runtime_panel
    render_runtime_panel(engine)


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
    from core.analytics import TYPE_LABELS, daily_analytics
    render_header("Аналитика посещений", "Сотрудники и ученики · время Ташкента")
    cols = st.columns([1, 1, 2])
    selected_date = cols[0].date_input("Дата", value=local_today(), format="DD.MM.YYYY")
    kind = cols[1].selectbox("Категория", [None, *TYPE_LABELS], format_func=lambda value: TYPE_LABELS.get(value, "Все"))
    search = cols[2].text_input("Поиск по имени", key="analytics-search")
    filter_key = (selected_date.isoformat(), kind, search)
    if st.session_state.get("analytics-filter") != filter_key:
        st.session_state["analytics-page"] = 1
        st.session_state["analytics-filter"] = filter_key
    page_number = int(st.session_state.get("analytics-page", 1))
    result = daily_analytics(engine, selected_date, person_type=kind, search=search, page=page_number)
    metrics = st.columns(3)
    metrics[0].metric("Замечены за день", result["total"])
    metrics[1].metric("Фиксации входа", result["entries"])
    metrics[2].metric("Фиксации выхода", result["exits"])
    if not result["total"]:
        st.info("За выбранную дату и категорию фиксаций нет.")
        return
    figure = go.Figure()
    for name, field, color in (("Вход", "entries", "#108455"), ("Выход", "exits", "#487cad")):
        figure.add_bar(name=name, x=list(range(24)), y=[row[field] for row in result["hourly"]], marker_color=color)
    figure.update_layout(height=250, barmode="group", margin=dict(l=0, r=0, t=10, b=0),
                         paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                         xaxis=dict(tickmode="linear", dtick=2, title="Час"), yaxis=dict(title="Фиксации", rangemode="tozero"))
    st.plotly_chart(figure, use_container_width=True, config={"displayModeBar": False})
    if st.session_state.get("analytics-page", 1) != result["page"]:
        st.session_state["analytics-page"] = result["page"]
    st.number_input("Страница", min_value=1, max_value=result["pages"], step=1, key="analytics-page", width=180)
    st.dataframe(pd.DataFrame([{
        "ФИО": row["name"] or "Без имени", "Категория": TYPE_LABELS[row["person_type"]],
        "Первый вход": format_local(row["first_entry"], "%H:%M:%S") if row["first_entry"] else "—",
        "Последний выход": format_local(row["last_exit"], "%H:%M:%S") if row["last_exit"] else "—",
        "Входы": row["entries"], "Выходы": row["exits"], "ID": row["person_id"],
    } for row in result["rows"]]), hide_index=True, use_container_width=True)
    st.caption("Фиксации не равны отдельным визитам. Парные входы и выходы с длительностью доступны в разделе «Отчёты».")


def render_attendance_reports():
    from core.attendance_reports import build_excel, build_pdf, load_attendance_report

    render_header("Отчёты по посещениям", "Сотрудники и ученики · данные локальной PostgreSQL")
    today = local_today()
    period = st.selectbox("Период", ["Сегодня", "Вчера", "Последние 7 дней", "Текущий месяц", "Свой период"],
                          index=3, width=280)
    if period == "Сегодня":
        start = end = today
    elif period == "Вчера":
        start = end = today - timedelta(days=1)
    elif period == "Последние 7 дней":
        start, end = today - timedelta(days=6), today
    elif period == "Текущий месяц":
        start, end = today.replace(day=1), today
    else:
        start_col, end_col = st.columns(2, gap="small")
        with start_col:
            start = st.date_input("С", value=today, format="DD.MM.YYYY")
        with end_col:
            end = st.date_input("По", value=today, format="DD.MM.YYYY")
    options_col, branch_col = st.columns([1, 2], gap="small")
    with options_col:
        include_local = st.toggle("Локальные сотрудники", value=False)
    with branch_col:
        branch_name = st.text_input("Название филиала", value="Филиал EduSchool", max_chars=100)
    parameters = (start, end, include_local, branch_name)
    if st.button("Сформировать отчёт", type="primary", icon=":material/description:"):
        st.session_state.pop("attendance_report_bundle", None)
        try:
            with st.spinner("Формируем отчёт"):
                report = load_attendance_report(engine, start, end, include_local=include_local)
                pdf_bytes = build_pdf(report, branch_name=branch_name)
                excel_bytes = build_excel(report, branch_name=branch_name)
            st.session_state["attendance_report_bundle"] = (parameters, report, pdf_bytes, excel_bytes)
        except ValueError as error:
            st.error(str(error))
        except Exception:
            st.error("Не удалось сформировать отчёт. Проверьте логи UI и повторите попытку.")

    bundle = st.session_state.get("attendance_report_bundle")
    if not bundle or bundle[0] != parameters:
        return
    _, report, pdf_bytes, excel_bytes = bundle
    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
    metrics = st.columns(4)
    metrics[0].metric("Сотрудники", report.employee_count)
    metrics[1].metric("Ученики", report.student_count)
    metrics[2].metric("Завершённые визиты", report.completed_count)
    metrics[3].metric("Неполные данные", report.incomplete_count)
    if report.incomplete_count:
        st.warning("Есть входы без выхода или выходы без входа. Проверьте детальный журнал.")
    filename = f"vision-office-attendance-{start:%Y%m%d}-{end:%Y%m%d}"
    pdf_col, excel_col = st.columns(2, gap="small")
    with pdf_col:
        st.download_button("Скачать PDF для руководителя", pdf_bytes, file_name=f"{filename}.pdf",
                           mime="application/pdf", icon=":material/picture_as_pdf:", use_container_width=True)
    with excel_col:
        st.download_button("Скачать подробный Excel", excel_bytes, file_name=f"{filename}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           icon=":material/table_view:", use_container_width=True)

    st.subheader("По дням")
    st.dataframe(pd.DataFrame([{
        "Дата": row.day.strftime("%d.%m.%Y"), "Сотрудники": row.employees,
        "Ученики": row.students, "Завершённые визиты": row.completed_visits,
        "Фиксации": row.observations,
    } for row in report.days]), use_container_width=True, hide_index=True)
    staff_tab, students_tab, visits_tab, events_tab = st.tabs(["Сотрудники", "Ученики", "Визиты", "Все фиксации"])
    for tab, is_student in ((staff_tab, False), (students_tab, True)):
        with tab:
            people = [row for row in report.people if (row.person_type == "eduschool_student") == is_student]
            st.caption(f"Найдено: {len(people)} · показаны первые 100")
            if not people:
                st.info("За выбранный период фиксаций нет.")
                continue
            st.dataframe(pd.DataFrame([{
                "ФИО": row.name, "Источник": "Локальный" if row.person_type == "local_employee" else "EduSchool",
                "Дней": row.days, "Визитов": row.visits,
                "Время, ч:м": f"{row.completed_minutes // 60:02d}:{row.completed_minutes % 60:02d}" if row.visits else "—",
                "Первый вход": format_local(row.first_entry) if row.first_entry else "—",
                "Последний выход": format_local(row.last_exit) if row.last_exit else "—",
                "Неполные": row.incomplete,
            } for row in people[:100]]), use_container_width=True, hide_index=True)
    with visits_tab:
        st.caption(f"Найдено: {len(report.visits)} · показаны первые 100")
        st.dataframe(pd.DataFrame([{
            "ФИО": row.name, "Дата": row.day.strftime("%d.%m.%Y"),
            "Вход": format_local(row.entry_at) if row.entry_at else "—",
            "Выход": format_local(row.exit_at) if row.exit_at else "—",
            "Состояние": {"complete": "Завершён", "no_exit": "Нет выхода", "no_entry": "Нет входа"}[row.status],
        } for row in report.visits[:100]]), use_container_width=True, hide_index=True)
    with events_tab:
        st.caption(f"Всего сохранённых фиксаций: {len(report.events)} · показаны первые 100")
        st.dataframe(pd.DataFrame([{
            "Время": row.local_at.strftime("%d.%m.%Y %H:%M:%S"), "ФИО": row.name,
            "Тип": "Ученик" if row.person_type == "eduschool_student" else "Сотрудник",
            "Событие": "Вход" if row.direction == "entry" else "Выход",
            "Камера": row.camera_id, "Отметка": row.mark,
        } for row in report.events[:100]]), use_container_width=True, hide_index=True)


def unknown_state_label(state):
    return {"active": "Требует проверки", "converted": "Зарегистрирован", "archived": "Архив"}.get(state, state)


def render_unknown_visitor_catalog():
    """Render local candidate visitors without touching ERP or camera processing."""
    session = Session()
    try:
        active_count = session.query(UnknownVisitor).filter_by(state="active").count()
        converted_count = session.query(UnknownVisitor).filter_by(state="converted").count()
        total_count = session.query(UnknownVisitor).count()
        first, second, third = st.columns(3)
        first.metric("Требуют проверки", active_count)
        second.metric("Зарегистрированы", converted_count)
        third.metric("Карточек", total_count)
        st.caption("Группы формируются локально по высокой похожести лица. Фото и биометрические шаблоны не передаются в ERP.")
        if not total_count:
            st.info("Каталог заполняется в фоне из новых и сохранённых неизвестных событий.")
            return

        controls, filter_col = st.columns([3, 1], gap="small", vertical_alignment="bottom")
        with controls:
            search = st.text_input("Поиск неизвестных", placeholder="Номер карточки", key="unknown_people_search").strip()
        with filter_col:
            state_filter = st.selectbox("Статус", ["Все", "Требует проверки", "Зарегистрирован", "Архив"], key="unknown_people_state")
        state_values = {"Требует проверки": "active", "Зарегистрирован": "converted", "Архив": "archived"}
        query = session.query(UnknownVisitor)
        if search:
            number = search.replace("Неизвестный", "").replace("#", "").strip()
            query = query.filter(UnknownVisitor.id == int(number)) if number.isdigit() else query.filter(UnknownVisitor.id == -1)
        if state_filter != "Все":
            query = query.filter(UnknownVisitor.state == state_values[state_filter])
        filtered_count = query.count()
        page_count = max(1, (filtered_count + 24) // 25)
        filter_key = (search, state_filter)
        if st.session_state.get("unknown_visitor_filters") != filter_key or st.session_state.get("unknown_visitors_page", 1) > page_count:
            st.session_state["unknown_visitors_page"] = 1
            st.session_state["unknown_visitor_filters"] = filter_key
        count_col, page_col = st.columns([3, 1], gap="small", vertical_alignment="bottom")
        with count_col:
            st.caption(f"Найдено: {filtered_count}")
        with page_col:
            page = st.selectbox("Страница", list(range(1, page_count + 1)),
                                format_func=lambda value: f"{value} / {page_count}", key="unknown_visitors_page")
        visitors = query.order_by(UnknownVisitor.last_seen_at.desc()).offset((page - 1) * 25).limit(25).all()
    finally:
        session.close()
    if not visitors:
        st.info("По этому запросу карточек нет.")
        return
    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
    visitor = render_person_picker(
        visitors, "unknown_visitor", lambda item: item.id,
        lambda item: f"Неизвестный #{item.id:04d}",
        lambda item: f"{item.visit_count} посещ.",
    )
    session = Session()
    try:
        observations = session.query(UnknownFaceObservation, RecognitionEvent).join(
            RecognitionEvent, RecognitionEvent.id == UnknownFaceObservation.event_id
        ).filter(
            UnknownFaceObservation.visitor_id == visitor.id,
        ).order_by(UnknownFaceObservation.observed_at.desc()).all()
        linked_employee = session.get(Employee, visitor.local_employee_id) if visitor.local_employee_id else None
        active_candidates = session.query(UnknownVisitor.id, UnknownVisitor.visit_count).filter(
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
    render_header("Сотрудники", "Основной каталог EduSchool, FaceID и события присутствия")
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
            unknown_visitor_count = session.query(UnknownVisitor).filter(UnknownVisitor.state == "active").count()
            eduschool_employee_count = session.query(EduSchoolCatalogPerson).filter_by(person_type="employee").count()
            eduschool_student_count = session.query(EduSchoolCatalogPerson).filter_by(person_type="student").count()
            eduschool_active_employee_count = session.query(EduSchoolCatalogPerson).filter_by(
                person_type="employee", active=True
            ).count()
            eduschool_face_ready_count = session.query(
                func.count(func.distinct(EduSchoolCatalogPerson.id))
            ).join(
                EduSchoolReferencePhoto,
                EduSchoolReferencePhoto.person_id == EduSchoolCatalogPerson.id,
            ).filter(
                EduSchoolCatalogPerson.person_type == "employee",
                EduSchoolCatalogPerson.active.is_(True),
                EduSchoolReferencePhoto.active.is_(True),
            ).scalar() or 0
            eduschool_attendance_ready_count = session.query(EduSchoolCatalogPerson).filter_by(
                person_type="employee", active=True, attendance_approved=True
            ).count()
        finally:
            session.close()
        status_names = {
            "ready": "Готов",
            "pending": "Обрабатывается",
            "invalid": "Требуется фото",
        }
        first, second, third, fourth = st.columns(4)
        first.metric("Сотрудники EduSchool", eduschool_employee_count)
        second.metric("Активны в филиале", eduschool_active_employee_count)
        third.metric("FaceID готов", eduschool_face_ready_count)
        fourth.metric("Отправка посещений", eduschool_attendance_ready_count)
        tabs = st.tabs(
            [
                f"EduSchool · {eduschool_employee_count + eduschool_student_count}",
                f"Старый ERP · {len(people)}",
                f"Локальная база · {len(local_employees)}",
                f"Неизвестные · {unknown_visitor_count}",
            ],
            key="people_catalog_tab",
            on_change="rerun",
        )
        eduschool_tab, erp_tab, local_tab, unknown_tab = tabs
        if eduschool_tab.open:
            with eduschool_tab:
                render_eduschool_directory()
            return
        if unknown_tab.open:
            with unknown_tab:
                render_unknown_visitor_catalog()
            return
        with erp_tab:
            from ui.catalog_transfer import render_catalog_transfer
            render_catalog_transfer(engine, edge_settings, people)
            if not people:
                st.info("Каталог старого ERP ещё не загружен в локальный кэш.")
            else:
                st.caption("Второстепенный каталог старого ERP. Локальные дополнительные фото не изменяют его карточки.")
                search = st.text_input("Поиск в старом ERP", placeholder="Имя или тип", key="erp_people_search")
                search_value = search.strip().casefold()
                filtered_people = [person for person in people if not search_value or search_value in (person.fio or "").casefold() or search_value in person.person_type.casefold()]
                if filtered_people:
                    list_col, detail_col = st.columns([1.05, 0.95], gap="medium", vertical_alignment="top")
                    with list_col, st.container(key="legacy-employee-list"):
                        st.markdown(f"<div class='directory-list-heading'><strong>Сотрудники</strong><span>{len(filtered_people)} найдено</span></div>",
                                    unsafe_allow_html=True)
                        person = render_person_picker(
                            filtered_people, "erp_people", lambda item: item.id,
                            lambda item: item.fio or f"{item.person_type} {item.id[:8]}",
                            lambda item: status_names.get(item.embedding_status, item.embedding_status),
                        )
                    with detail_col, st.container(key="legacy-employee-detail"):
                        session = Session()
                        try:
                            events = session.query(RecognitionEvent).filter(
                                RecognitionEvent.person_id == person.id
                            ).order_by(RecognitionEvent.created_at.desc()).limit(12).all()
                        finally:
                            session.close()
                        render_profile_header(
                            person.fio or f"{person.person_type} {person.id[:8]}",
                            "Старый ERP-каталог", f"ERP {person.id[:8]}", person.photo_path,
                            person.person_type, status_names.get(person.embedding_status, person.embedding_status),
                            f"основное + {photo_counts.get(person.id, 0)} доп.",
                        )
                        render_event_history(events, "У этого ERP-сотрудника пока нет локальных событий распознавания.")
                else:
                    st.caption("Поиск не нашёл сотрудников.")
        with local_tab:
            if not local_employees:
                st.caption("Локальных сотрудников пока нет. Их можно добавить на вкладке «Регистрация».")
            else:
                st.caption("Эти профили и их посещаемость остаются на устройстве и не передаются в ERP.")
                search = st.text_input("Поиск в локальной базе", placeholder="Имя или роль", key="local_people_search")
                search_value = search.strip().casefold()
                filtered_employees = [employee for employee in local_employees if not search_value or search_value in employee.full_name.casefold() or search_value in (employee.role or "").casefold()]
                if filtered_employees:
                    list_col, detail_col = st.columns([1.05, 0.95], gap="medium", vertical_alignment="top")
                    with list_col, st.container(key="local-employee-list"):
                        st.markdown(f"<div class='directory-list-heading'><strong>Локальные сотрудники</strong><span>{len(filtered_employees)} найдено</span></div>",
                                    unsafe_allow_html=True)
                        employee = render_person_picker(
                            filtered_employees, "local_people", lambda item: item.id,
                            lambda item: item.full_name,
                            lambda item: "Готов" if item.face_embeddings else "Нет фото",
                        )
                    with detail_col, st.container(key="local-employee-detail"):
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
                            employee.full_name, "Локальная PostgreSQL", f"Профиль #{employee.id:04d}",
                            employee.photo_path, employee.role or "Не указана",
                            "Готов" if employee.face_embeddings else "Нет", "локальное",
                        )
                        render_local_attendance_history(attendance)
                        render_event_history(events, "Событий камеры для этого локального профиля пока нет.")
                else:
                    st.caption("Поиск не нашёл сотрудников.")
        return
    render_eduschool_directory()
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


def render_eduschool_directory():
    from core.eduschool.catalog import load_settings
    from core.eduschool.photos import EduSchoolPhotoService, recognition_id

    settings = load_settings()
    if not settings.enabled:
        return
    st.subheader("Каталог EduSchool")
    session = Session()
    try:
        state = session.get(EduSchoolCatalogSyncState, 1)
        if state and state.last_success_at:
            st.caption(
                f"Последнее обновление: {format_local(state.last_success_at)} · "
                f"Студентов: {state.student_count} · Сотрудников: {state.employee_count}"
            )
        else:
            st.info("Ожидается первая синхронизация EduSchool.")
        if state and state.last_error:
            st.warning(f"Ошибка синхронизации EduSchool: {state.last_error}")
        with st.container(key="employee-browser"):
            list_col, detail_col = st.columns([1.05, 0.95], gap="medium", vertical_alignment="top")
            with list_col, st.container(key="employee-list"):
                person = render_eduschool_list(session)
            if person is not None:
                with detail_col, st.container(key="employee-detail"):
                    local_photos = session.query(EduSchoolReferencePhoto).filter_by(
                        person_id=person.id, active=True,
                    ).order_by(EduSchoolReferencePhoto.created_at, EduSchoolReferencePhoto.id).all()
                    render_eduschool_profile(session, person, local_photos, settings, recognition_id, EduSchoolPhotoService)
    finally:
        session.close()


def render_eduschool_list(session):
    person_type = st.segmented_control(
        "Каталог", ["Сотрудники", "Студенты"], default="Сотрудники",
        key="eduschool_directory_type", width="stretch", label_visibility="collapsed",
    )
    search_col, filter_col = st.columns([2, 1.3], gap="small", vertical_alignment="bottom")
    with search_col:
        search = st.text_input(
            "Поиск", placeholder="ФИО, номер или ID", key="eduschool_directory_search",
            icon=":material/search:", label_visibility="collapsed",
        ).strip()
    active_filters = sum(st.session_state.get(key, "Все") != "Все" for key in ("eduschool_branch_filter", "eduschool_face_filter"))
    filter_label = f"Фильтры · {active_filters}" if active_filters else "Фильтры"
    with filter_col, st.popover(filter_label, icon=":material/tune:", use_container_width=True, key="eduschool_filters_popover"):
        branch_filter = st.selectbox("Филиал", ["Все", "В филиале", "Вне филиала"], key="eduschool_branch_filter")
        face_choice = st.selectbox("FaceID", ["Все", "Готов", "Нет фото"], key="eduschool_face_filter")
        if st.button("Сбросить фильтры", icon=":material/restart_alt:", use_container_width=True):
            st.session_state["eduschool_branch_filter"] = "Все"
            st.session_state["eduschool_face_filter"] = "Все"
            st.rerun()
    kind = "employee" if person_type == "Сотрудники" else "student"
    query = session.query(EduSchoolCatalogPerson).filter(EduSchoolCatalogPerson.person_type == kind)
    if search:
        pattern = f"%{search}%"
        query = query.filter(or_(
            EduSchoolCatalogPerson.full_name.ilike(pattern),
            EduSchoolCatalogPerson.employee_no.ilike(pattern),
            EduSchoolCatalogPerson.external_id.ilike(pattern),
        ))
    if branch_filter != "Все":
        query = query.filter(EduSchoolCatalogPerson.active.is_(branch_filter == "В филиале"))
    has_photo = session.query(EduSchoolReferencePhoto.id).filter(
        EduSchoolReferencePhoto.person_id == EduSchoolCatalogPerson.id,
        EduSchoolReferencePhoto.active.is_(True),
    ).exists()
    if face_choice != "Все":
        query = query.filter(has_photo if face_choice == "Готов" else ~has_photo)
    total = query.count()
    count_col, size_col, page_col = st.columns([1.6, 1, 1], gap="small", vertical_alignment="bottom")
    with count_col:
        st.markdown(f"<div class='directory-list-heading'><strong>{html.escape(person_type)}</strong><span>{total} найдено</span></div>",
                    unsafe_allow_html=True)
    with size_col:
        page_size = st.selectbox("На странице", [25, 50], key="eduschool_directory_page_size")
    page_count = max(1, (total + page_size - 1) // page_size)
    page_key = "eduschool_directory_page"
    filter_key = (kind, search, branch_filter, face_choice, page_size)
    if st.session_state.get("eduschool_directory_filters") != filter_key or st.session_state.get(page_key, 1) > page_count:
        st.session_state[page_key] = 1
        st.session_state["eduschool_directory_filters"] = filter_key
    with page_col:
        page = st.selectbox("Страница", list(range(1, page_count + 1)),
                            format_func=lambda value: f"{value} / {page_count}", key=page_key)
    rows = query.order_by(EduSchoolCatalogPerson.full_name, EduSchoolCatalogPerson.id).offset(
        (page - 1) * page_size
    ).limit(page_size).all()
    if not rows:
        st.info("По этому запросу никого не найдено.")
        if active_filters:
            if st.button("Сбросить фильтры", key="eduschool_empty_reset", icon=":material/restart_alt:"):
                st.session_state["eduschool_branch_filter"] = "Все"
                st.session_state["eduschool_face_filter"] = "Все"
                st.rerun()
        return None
    ids = [row.id for row in rows]
    photo_counts = dict(session.query(
        EduSchoolReferencePhoto.person_id, func.count(EduSchoolReferencePhoto.id),
    ).filter(
        EduSchoolReferencePhoto.person_id.in_(ids), EduSchoolReferencePhoto.active.is_(True),
    ).group_by(EduSchoolReferencePhoto.person_id).all())
    previous_id = st.session_state.get("eduschool_selected_person_id")
    person = next((row for row in rows if row.id == previous_id), rows[0])
    st.session_state["eduschool_selected_person_id"] = person.id
    with st.container(height=590, border=False, key="employee-rows"):
        for row in rows:
            name_col, face_col = st.columns([3, 1], gap="small", vertical_alignment="center")
            with name_col:
                if st.button(
                    display_name(row.full_name), key=f"eduschool_open_{row.id}",
                    type="primary" if row.id == person.id else "secondary",
                    use_container_width=True,
                ):
                    st.session_state["eduschool_selected_person_id"] = row.id
                    st.rerun()
            with face_col:
                ready = bool(row.active and photo_counts.get(row.id))
                st.markdown(
                    f"<span class='directory-face{' ready' if ready else ''}'>"
                    f"{'Готов' if ready else 'Нет фото'}</span>",
                    unsafe_allow_html=True,
                )
    return person


def render_eduschool_profile(session, person, local_photos, settings, recognition_id, photo_service):
    events = session.query(RecognitionEvent).filter(
        RecognitionEvent.person_id == recognition_id(person)
    ).order_by(RecognitionEvent.created_at.desc()).limit(25).all()
    render_profile_header(
        person.full_name, "EduSchool", person.external_id,
        local_photos[0].photo_path if local_photos else None,
        "Сотрудник" if person.person_type == "employee" else "Студент",
        "FaceID готов" if person.active and local_photos else "Нет FaceID",
        "В филиале" if person.active else "Вне филиала",
    )
    sections = ["Обзор", "События", "Фото"]
    if person.person_type == "employee":
        sections.append("Отправка")
    section = st.segmented_control(
        "Карточка", sections, default="Обзор", key=f"eduschool_profile_section_{person.person_type}",
        width="stretch", label_visibility="collapsed",
    )
    if section == "Обзор":
        last_event = events[0] if events else None
        facts = [
            ("Состояние", {"active": "Активен", "new": "Новый", "archived": "Архив"}.get(
                person.source_status, person.source_status)),
            ("ID EduSchool", person.external_id),
            ("Табельный номер", person.employee_no or "Не указан") if person.person_type == "employee" else ("Тип", "Студент"),
            ("Фото FaceID", str(len(local_photos))),
            ("Последний проход", format_local(last_event.created_at) if last_event else "Нет событий"),
            ("Камера", last_event.camera_id if last_event else "—"),
        ]
        st.markdown(
            "<div class='profile-facts'>" + "".join(
                f"<div><span>{html.escape(label)}</span><strong>{html.escape(str(value))}</strong></div>"
                for label, value in facts
            ) + "</div>", unsafe_allow_html=True,
        )
        if person.person_type == "employee":
            turnstile_state = session.get(EduSchoolTurnstileState, 1)
            delivery = "Приостановлено" if person.attendance_blocked else (
                "Готово к отправке" if person.attendance_approved and turnstile_state and turnstile_state.enabled
                else "Не готово к отправке"
            )
            st.caption(f"Посещения EduSchool: {delivery}")
        if person.source_photo_status in ("invalid", "failed"):
            st.warning(f"Фото API: {person.source_photo_error or 'не удалось обработать'}")
    elif section == "События":
        render_event_history(events, "Этот человек пока не был распознан камерой.")
    elif section == "Фото":
        if local_photos:
            columns = st.columns(min(2, len(local_photos)))
            for index, photo in enumerate(local_photos):
                path = profile_photo_path(photo.photo_path)
                if path:
                    with columns[index % len(columns)]:
                        st.image(str(path), use_container_width=True, caption=f"FaceID · {index + 1}")
        else:
            st.caption("Фото FaceID пока нет.")
        with st.form(f"eduschool_reference_photo_form_{person.id}", clear_on_submit=True):
            uploaded = st.file_uploader("Добавить локальное фото", type=["jpg", "jpeg", "png"])
            submitted = st.form_submit_button("Добавить фото для FaceID", disabled=not person.active)
        if submitted:
            if uploaded is None:
                st.warning("Выберите фото с одним хорошо видимым лицом.")
            else:
                try:
                    with st.spinner("Проверяем лицо и сохраняем локальный шаблон"):
                        photo_service(reference_limit=settings.local_reference_photo_limit).add_local_photo(
                            person.id, uploaded.getvalue(),
                            lambda image: get_recognizer().get_embedding(image, require_single=True),
                        )
                    st.success("Фото сохранено. Камера обновит FaceID-кэш в течение нескольких секунд.")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
                except Exception:
                    st.error("Не удалось сохранить фото. Проверьте логи и повторите попытку.")
    elif section == "Отправка":
        from core.eduschool.turnstile import reconcile_ambiguous, set_person_hold

        turnstile_state = session.get(EduSchoolTurnstileState, 1)
        delivery_on = bool(turnstile_state and turnstile_state.enabled)
        attendance_status = (
            "Приостановлено" if person.attendance_blocked else
            "Готов автоматически" if person.attendance_approved else
            "Ожидает номер или фото FaceID"
        )
        st.caption(f"Отправка: {'включена' if delivery_on else 'выключена'} · {attendance_status}")
        blocked = st.toggle("Приостановить отправку для этого сотрудника", value=person.attendance_blocked,
                            key=f"eduschool_hold_{person.id}")
        if blocked != person.attendance_blocked:
            set_person_hold(person.id, blocked, engine)
            st.rerun()
        deliveries = session.query(EduSchoolTurnstileOutbox).filter_by(person_id=person.id).order_by(
            EduSchoolTurnstileOutbox.created_at.desc()
        ).limit(12).all()
        if deliveries:
            st.dataframe(pd.DataFrame([{
                "Время": format_local(item.sent_at or item.created_at, "%d.%m %H:%M"),
                "Событие": item.event_id[:8], "Статус": {
                    "sent": "Отправлено", "pending": "В очереди", "retry": "Повтор",
                    "sending": "Отправляется", "failed": "Ошибка", "ambiguous": "Проверить",
                    "skipped": "Пропущено",
                }.get(item.status, item.status),
                "Попытки": item.attempts, "Код": item.response_code,
                "ID EduSchool": item.backend_event_id or "",
                "Причина": item.last_error or "",
            } for item in deliveries]), use_container_width=True, hide_index=True)
        else:
            st.caption("Отправок пока нет.")
        uncertain = [item for item in deliveries if item.status == "ambiguous"]
        if uncertain:
            st.warning("Исход отправки неизвестен. Проверьте запись в EduSchool перед ручным повтором.")
            with st.form(f"eduschool_reconcile_{person.id}"):
                chosen = st.selectbox("Событие для сверки", uncertain, format_func=lambda item: item.event_id[:8])
                outcome = st.radio("Результат сверки в EduSchool", ["Запись уже есть", "Записи нет"], horizontal=True)
                checked = st.checkbox("Я проверил(а) запись в EduSchool")
                reconcile = st.form_submit_button("Сохранить результат сверки")
            if reconcile:
                if not checked:
                    st.warning("Подтвердите сверку перед изменением статуса.")
                else:
                    try:
                        reconcile_ambiguous(chosen.event_id, already_delivered=outcome == "Запись уже есть", engine=engine)
                        st.rerun()
                    except ValueError as error:
                        st.error(str(error))


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


def render_delivery_log():
    render_header("Отправки API", "Журнал посещений EduSchool и ответов сервера")
    st.caption("Здесь показаны фактические попытки POST. События в очереди без попытки ещё не отправлялись.")

    period_col, status_col, search_col = st.columns([1, 1, 2], gap="small")
    with period_col:
        period = st.selectbox("Период", ["24 часа", "7 дней", "30 дней", "Всё время"], index=1)
    with status_col:
        status_label = st.selectbox("Результат", ["Все", "Отправлено", "Повтор", "Ошибка", "Неясный исход"])
    with search_col:
        search = st.text_input("Поиск", placeholder="ФИО, табельный номер или ID события").strip()

    status_values = {
        "Отправлено": ("sent",), "Повтор": ("retry",),
        "Ошибка": ("blocked",), "Неясный исход": ("ambiguous", "sending"),
    }
    status_names = {
        "sent": "Отправлено", "retry": "Ожидает повтора", "blocked": "Ошибка",
        "ambiguous": "Неясный исход", "sending": "В процессе",
    }
    days = {"24 часа": 1, "7 дней": 7, "30 дней": 30}
    with Session() as session:
        base = session.query(EduSchoolDeliveryAttempt).join(
            EduSchoolTurnstileOutbox, EduSchoolTurnstileOutbox.event_id == EduSchoolDeliveryAttempt.event_id
        ).join(RecognitionEvent, RecognitionEvent.id == EduSchoolDeliveryAttempt.event_id).outerjoin(
            EduSchoolCatalogPerson, EduSchoolCatalogPerson.id == EduSchoolTurnstileOutbox.person_id
        )
        if period in days:
            base = base.filter(EduSchoolDeliveryAttempt.started_at >= datetime.now(timezone.utc) - timedelta(days=days[period]))
        totals = dict(base.with_entities(EduSchoolDeliveryAttempt.status, func.count(EduSchoolDeliveryAttempt.id))
                      .group_by(EduSchoolDeliveryAttempt.status).all())
        queued = session.query(EduSchoolTurnstileOutbox).filter(
            EduSchoolTurnstileOutbox.status.in_(("pending", "retry"))
        ).count()
        metrics = st.columns(4)
        metrics[0].metric("Отправлено", totals.get("sent", 0))
        metrics[1].metric("Ожидает повтора", totals.get("retry", 0))
        metrics[2].metric("Ошибка", totals.get("blocked", 0))
        metrics[3].metric("Неясный исход", totals.get("ambiguous", 0) + totals.get("sending", 0))
        st.caption(f"В очереди сейчас: {queued}. Коды и статус отражают ответ API; при неясном исходе проверьте EduSchool перед повтором.")

        if status_label in status_values:
            base = base.filter(EduSchoolDeliveryAttempt.status.in_(status_values[status_label]))
        if search:
            needle = f"%{search.lower()}%"
            base = base.filter(or_(
                func.lower(EduSchoolCatalogPerson.full_name).like(needle),
                func.lower(EduSchoolCatalogPerson.employee_no).like(needle),
                func.lower(EduSchoolDeliveryAttempt.event_id).like(needle),
            ))
        total = base.count()
        if total == 0:
            st.info("За выбранный период попыток отправки не найдено.")
            return
        pages = max(1, (total + 24) // 25)
        page = st.selectbox("Страница", range(1, pages + 1), format_func=lambda value: f"{value} / {pages}") if pages > 1 else 1
        rows = base.with_entities(
            EduSchoolDeliveryAttempt, RecognitionEvent.person_name,
            EduSchoolCatalogPerson.full_name, EduSchoolCatalogPerson.employee_no,
            EduSchoolTurnstileOutbox.status, RecognitionEvent.event_type,
        ).order_by(EduSchoolDeliveryAttempt.started_at.desc(), EduSchoolDeliveryAttempt.id.desc()
                   ).offset((page - 1) * 25).limit(25).all()

        st.caption(f"Найдено: {total}")
        st.dataframe(pd.DataFrame([{
            "Время": format_local(attempt.started_at, "%d.%m.%Y %H:%M:%S"),
            "Сотрудник": full_name or person_name or "—",
            "Номер": employee_no or "—",
            "Направление": "Вход" if event_type == "entry" else "Выход",
            "Адрес": attempt.endpoint,
            "HTTP": attempt.http_status,
            "Код API": attempt.api_code,
            "Результат": status_names.get(attempt.status, attempt.status),
            "Попытка": attempt.attempt_number,
            "Причина": attempt.error or "",
        } for attempt, person_name, full_name, employee_no, _, event_type in rows]),
            use_container_width=True, hide_index=True)

        with st.expander("Детали попытки"):
            selected = st.selectbox(
                "Запись", rows,
                format_func=lambda row: (
                    f"{format_local(row[0].started_at, '%d.%m %H:%M:%S')} · "
                    f"{row[2] or row[1] or 'Сотрудник'} · {status_names.get(row[0].status, row[0].status)}"
                ),
            )
            attempt, _, _, _, queue_status, _ = selected
            st.code(attempt.endpoint, language=None)
            st.write(f"Событие: `{attempt.event_id}` · попытка №{attempt.attempt_number}")
            st.write(f"Начало: {format_local(attempt.started_at)} · завершение: "
                     f"{format_local(attempt.finished_at) if attempt.finished_at else 'исход не зафиксирован'}")
            st.write(f"HTTP: {attempt.http_status if attempt.http_status is not None else 'нет ответа'} · "
                     f"код API: {attempt.api_code if attempt.api_code is not None else 'нет'} · "
                     f"очередь: {queue_status}")
            if attempt.backend_event_id:
                st.write(f"ID EduSchool: `{attempt.backend_event_id}`")
            if attempt.duplicate:
                st.caption("Сервер отметил запись как дубликат.")
            if attempt.error:
                st.warning(attempt.error)


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
elif page == "Камеры":
    from ui.cameras import render_cameras
    render_header("Камеры", "Потоки, направление прохода и производительность")
    render_cameras(engine)
elif page == "Аналитика":
    render_analytics()
elif page == "Отчёты":
    render_attendance_reports()
elif page == "Сотрудники":
    render_people()
elif page == "Регистрация":
    render_registration()
elif page == "Отправки":
    render_delivery_log()
else:
    render_developer_api()
