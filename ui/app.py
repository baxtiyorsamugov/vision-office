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
from database.models import AccessLogOutbox, Attendance, Base, EdgeSyncState, Employee, HealthIncident, RecognitionEvent, RemotePerson, RemotePersonReferencePhoto
from core.edge.config import load_edge_settings
from core.performance import read_runtime_status


st.set_page_config(page_title="Vision Office", page_icon="VO", layout="wide", initial_sidebar_state="collapsed")


def apply_theme():
    st.markdown(
        """
        <style>
        :root { --ink:#171a1f; --muted:#777b82; --line:#e9e9e9; --surface:#ffffff; --canvas:#e4e3e7; --green:#138b5c; --green-dark:#0d714b; --green-soft:#e8f5ee; --warning:#ad7500; }
        #MainMenu, footer, [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display:none!important; }
        [data-testid="stAppDeployButton"] { display:none; }
        .stApp { background:var(--canvas); color:var(--ink); }
        [data-testid="stHeader"] { height:0; background:transparent; }
        [data-testid="stMain"] { padding:0 18px 34px; }
        .block-container { max-width:1320px; margin:42px auto 0; padding:20px 26px 32px; background:#f5f5f5; border:1px solid rgba(255,255,255,.9); border-radius:18px; box-shadow:0 18px 42px rgba(25,29,35,.08); }
        h1,h2,h3 { color:var(--ink)!important; letter-spacing:0!important; }
        h1 { font-size:30px!important; font-weight:650!important; line-height:1.18!important; margin:30px 0 4px!important; }
        h2 { font-size:16px!important; font-weight:650!important; line-height:1.35!important; margin:26px 0 10px!important; }
        h3 { font-size:15px!important; font-weight:650!important; }
        p,[data-testid="stCaptionContainer"] { color:var(--muted); }
        [data-testid="stCaptionContainer"] { font-size:13px; }
        .brand { display:flex; align-items:center; gap:9px; white-space:nowrap; padding-left:4px; }
        .brand-mark { width:31px; height:31px; display:inline-flex; align-items:center; justify-content:center; background:var(--green); color:#fff; border-radius:50%; font-size:11px; font-weight:800; box-shadow:inset 0 0 0 5px rgba(255,255,255,.18); }
        .brand-name { color:var(--green); font-size:18px; font-weight:750; }
        .profile-dot { width:34px; height:34px; display:flex; align-items:center; justify-content:center; margin-left:auto; background:#1f2b32; color:#fff; border:3px solid #fff; border-radius:50%; font-size:11px; font-weight:700; box-shadow:0 2px 8px rgba(20,28,35,.12); }
        [data-testid="stVerticalBlockBorderWrapper"] { background:var(--surface); border:1px solid var(--line)!important; border-radius:18px!important; box-shadow:none!important; }
        [data-testid="stVerticalBlockBorderWrapper"] > div { padding:18px!important; }
        [data-testid="stRadio"] > div { display:flex; align-items:center; justify-content:center; gap:2px; padding:4px; background:#f4f4f4; border-radius:999px; }
        [data-testid="stRadio"] label { width:auto!important; margin:0!important; padding:8px 12px!important; border-radius:999px!important; color:#53565b!important; font-size:12px!important; font-weight:500!important; white-space:nowrap; }
        [data-testid="stRadio"] label:has(input:checked) { background:#fff!important; color:var(--ink)!important; box-shadow:0 1px 4px rgba(26,30,35,.08); }
        [data-testid="stRadio"] label:hover { background:#fff!important; }
        [data-testid="stRadio"] [data-baseweb="radio"] > div:first-child, [data-testid="stRadio"] input { display:none!important; }
        [data-testid="stMetric"] { min-height:108px; padding:18px!important; background:var(--surface); border:1px solid var(--line); border-radius:18px; }
        [data-testid="stMetricLabel"] { color:var(--muted); font-size:12px; font-weight:500; }
        [data-testid="stMetricValue"] { color:var(--ink); font-size:26px; font-weight:650; line-height:1.1; }
        [data-testid="stMetricDelta"] { color:var(--green)!important; font-size:12px; font-weight:600; }
        .status-strip { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:16px 18px; margin:16px 0 18px; border:1px solid var(--line); border-radius:18px; background:var(--surface); }
        .status-label { color:var(--muted); font-size:12px; margin-bottom:3px; }.status-value { color:var(--ink); font-weight:650; font-size:15px; }
        .badge { display:inline-flex; align-items:center; gap:7px; color:#50545a; font-size:12px; font-weight:600; }.dot { width:8px; height:8px; border-radius:50%; display:inline-block; }.dot-online { background:var(--green); box-shadow:0 0 0 4px var(--green-soft); }.dot-idle { background:var(--warning); }
        .panel-title { color:var(--ink); font-weight:650; font-size:15px; margin:0 0 4px; }.panel-note { color:var(--muted); font-size:12px; line-height:1.5; margin:0; }
        .stButton > button, .stLinkButton > a { min-height:40px; border-radius:999px; border:1px solid var(--green); background:var(--green); color:#fff; font-size:13px; font-weight:650; letter-spacing:0; transition:all .16s ease; }
        .stButton > button:hover, .stLinkButton > a:hover { background:var(--green-dark); border-color:var(--green-dark); color:#fff; transform:translateY(-1px); }
        .stButton > button[kind="secondary"] { background:#fff; color:var(--ink); border-color:var(--line); }.stButton > button[kind="secondary"]:hover { background:#f5f5f5; border-color:#d8d8d8; color:var(--ink); }
        .stTextInput input,.stSelectbox [data-baseweb="select"] > div,.stDateInput input,[data-testid="stFileUploaderDropzone"] { min-height:42px!important; background:#fff!important; border:1px solid var(--line)!important; border-radius:12px!important; box-shadow:none!important; }
        [data-testid="stDataFrame"] { border:1px solid var(--line); border-radius:14px; overflow:hidden; background:#fff; }
        [data-testid="stDataFrame"] [role="gridcell"] { font-size:13px!important; }
        .employee-head { display:flex; align-items:center; gap:16px; padding:5px 3px; }.employee-photo,.employee-fallback { width:82px; height:82px; border-radius:50%; object-fit:cover; border:4px solid #fff; box-shadow:0 3px 12px rgba(28,33,38,.1); }.employee-fallback { background:var(--green-soft); color:var(--green); display:flex; align-items:center; justify-content:center; font-size:25px; font-weight:700; }.employee-name { color:var(--ink); font-size:23px; font-weight:650; margin-bottom:3px; }.employee-role { color:var(--green); font-size:13px; font-weight:650; margin-bottom:7px; }.employee-meta { color:var(--muted); font-size:13px; }
        .empty-state { padding:48px 18px; text-align:center; color:var(--muted); border:1px dashed #d9d9d9; border-radius:18px; background:#fff; }.section-rule { border:0; border-top:1px solid var(--line); margin:26px 0; }
        .activity-meta { color:var(--muted); font-size:12px; text-align:right; line-height:1.5; }
        .stButton > button:focus-visible, [data-baseweb="select"] *:focus-visible { outline:3px solid rgba(19,139,92,.22)!important; outline-offset:2px; }
        [data-testid="stForm"] { padding:20px; background:var(--surface); border:1px solid var(--line); border-radius:18px; }
        [data-testid="stAlert"] { border-radius:14px; }
        @media (max-width: 860px) {
          [data-testid="stMain"] { padding:0 8px 20px; }
          .block-container { margin-top:8px; padding:12px; border-radius:14px; }
          .brand-name { font-size:16px; }
          [data-testid="stRadio"] > div { justify-content:flex-start; overflow-x:auto; }
          [data-testid="stRadio"] label { padding:7px 10px!important; }
          h1 { font-size:26px!important; margin-top:22px!important; }
          .status-strip { flex-wrap:wrap; border-radius:14px; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


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
        submitted = st.form_submit_button("Добавить локально", use_container_width=True)
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


def display_name(name):
    return name[:-4] if name.lower().endswith(".jpg") else name


def day_bounds(selected_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(selected_date, datetime.min.time())
    return start, start + timedelta(days=1)


def status_badge(running, active_label, idle_label):
    return f'<span class="badge"><span class="dot {"dot-online" if running else "dot-idle"}"></span>{active_label if running else idle_label}</span>'


def render_header(title, subtitle):
    st.title(title)
    st.caption(subtitle)


def render_navigation():
    with st.container(border=True):
        left, center, right = st.columns([1.55, 5.6, 0.45], gap="small", vertical_alignment="center")
        with left:
            st.markdown(
                '<div class="brand"><span class="brand-mark">VO</span><span class="brand-name">Vision Office</span></div>',
                unsafe_allow_html=True,
            )
        with center:
            st.markdown('<div class="top-navigation">', unsafe_allow_html=True)
            page = st.radio(
                "Навигация",
                ["Панель", "Аналитика", "Сотрудники", "Регистрация", "API"],
                horizontal=True,
                label_visibility="collapsed",
            )
            st.markdown("</div>", unsafe_allow_html=True)
        with right:
            st.markdown('<div class="profile-dot">VO</div>', unsafe_allow_html=True)
    return page


def render_control_center():
    render_header("Операционный центр", "Камеры, распознавание и регистрация присутствия")
    edge_settings = load_edge_settings()
    runtime = read_runtime_status() or {}
    live_running = bool(runtime.get("running")) if managed_runtime() else process_running("live_process")
    demo_running = process_running("demo_process")
    recognition_status = "Активно" if live_running else "Ожидание"
    st.markdown(f'''<div class="status-strip"><div><div class="status-label">Распознавание</div><div class="status-value">{recognition_status}</div></div><div>{status_badge(live_running, "RTSP подключён", "RTSP остановлен")}</div><div>{status_badge(demo_running, "Демо запущено", "Демо выключено")}</div><div class="activity-meta">Обновлено<br>{datetime.now().strftime("%H:%M")}</div></div>''', unsafe_allow_html=True)

    session = Session()
    try:
        employee_count = (
            session.query(RemotePerson).filter(RemotePerson.active.is_(True)).count() + session.query(Employee).count()
            if edge_settings.configured else session.query(Employee).count()
        )
        day_start, day_end = day_bounds(date.today())
        today_count = session.query(Attendance.employee_id).filter(
            Attendance.timestamp >= day_start, Attendance.timestamp < day_end,
        ).distinct().count()
        event_count = session.query(RecognitionEvent).filter(
            RecognitionEvent.created_at >= day_start, RecognitionEvent.created_at < day_end,
        ).count()
        active_incidents = session.query(HealthIncident).filter(HealthIncident.status == "open").count()
    finally:
        session.close()
    metrics = st.columns(4)
    metrics[0].metric("Сотрудники", employee_count)
    metrics[1].metric("Сегодня замечены", today_count)
    metrics[2].metric("События сегодня", event_count)
    camera_count = len((read_runtime_status() or {}).get("cameras", [])) or 1
    metrics[3].metric("Камеры", camera_count, "в работе" if live_running else "ожидание")
    if active_incidents:
        st.warning(f"Health Checker: активных инцидентов: {active_incidents}")

    render_performance_panel()

    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
    left, right = st.columns(2, gap="large")
    with left:
        with st.container(border=True):
            st.markdown("<p class='panel-title'>Основная камера</p><p class='panel-note'>RTSP-поток с записью присутствия</p><br>", unsafe_allow_html=True)
            if managed_runtime():
                st.success("Контейнер vision-worker управляет камерой через Docker Compose.")
                st.link_button(
                    "Открыть монитор камер",
                    f"{api_public_url()}/monitor",
                    use_container_width=True,
                )
                st.caption("Монитор читает локальные preview-кадры и не создаёт второе RTSP-подключение.")
            elif live_running:
                st.success("Поток запущен")
                if st.button("Остановить камеру", key="stop_live", type="secondary", use_container_width=True):
                    stop_process("live_process")
                    st.rerun()
            elif st.button("Запустить камеру", key="start_live", type="primary", use_container_width=True):
                start_process("live_process", "main.py")
                st.rerun()
    with right:
        with st.container(border=True):
            st.markdown("<p class='panel-title'>Тестовый контур</p><p class='panel-note'>Проверка распознавания на test.mp4</p><br>", unsafe_allow_html=True)
            if demo_running:
                st.info("Демо выполняется")
                if st.button("Остановить тест", key="stop_demo", type="secondary", use_container_width=True):
                    stop_process("demo_process")
                    st.rerun()
            elif st.button("Запустить тест", key="start_demo", type="secondary", use_container_width=True):
                start_process("demo_process", "test_video.py")
                st.rerun()

    st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
    st.subheader("Последние обнаружения")
    recent_events = load_recent_events(limit=8)
    if recent_events.empty:
        st.markdown("<div class='empty-state'>Новых событий пока нет.</div>", unsafe_allow_html=True)
    else:
        st.dataframe(recent_events, use_container_width=True, hide_index=True)


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
    return pd.DataFrame([{"ФИО": row.full_name, "Роль": row.role or "Не указана", "Дата и время": row.timestamp, "Событие": row.event_type or "check_in"} for row in rows])


def load_recent_events(limit):
    session = Session()
    try:
        rows = session.query(RecognitionEvent).order_by(RecognitionEvent.created_at.desc()).limit(limit).all()
    finally:
        session.close()
    return pd.DataFrame([{
        "ФИО": display_name(row.person_name or "Неизвестный"),
        "Роль": row.person_type,
        "Время": row.created_at.strftime("%d.%m %H:%M"),
        "Событие": row.event_type,
        "Камера": row.camera_id,
    } for row in rows])


def render_analytics():
    render_header("Аналитика присутствия", "Сводка входов и дисциплины по выбранной дате")
    selected_date = st.date_input("Дата", value=date.today(), label_visibility="collapsed")
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
    with chart_col:
        st.subheader("Приходы по часам")
        arrivals = go.Figure(go.Bar(x=hourly["Час"], y=hourly["Количество"], marker_color="#138b5c", hovertemplate="%{y} сотрудника<extra></extra>"))
        arrivals.update_layout(margin=dict(l=0, r=0, t=12, b=0), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", xaxis=dict(title=None, tickmode="linear", dtick=1, gridcolor="#e8edf2"), yaxis=dict(title=None, rangemode="tozero", gridcolor="#e8edf2"), showlegend=False)
        st.plotly_chart(arrivals, use_container_width=True, config={"displayModeBar": False})
    with status_col:
        st.subheader("Дисциплина")
        status_counts = first_events["Статус"].value_counts()
        discipline = go.Figure(go.Pie(labels=status_counts.index, values=status_counts.values, hole=.72, marker_colors=["#138b5c" if item == "Вовремя" else "#c65663" for item in status_counts.index], textinfo="none"))
        discipline.update_layout(margin=dict(l=0, r=0, t=12, b=0), paper_bgcolor="rgba(0,0,0,0)", showlegend=True)
        st.plotly_chart(discipline, use_container_width=True, config={"displayModeBar": False})
    st.subheader("Первое появление")
    first_events["Время"] = first_events["Дата и время"].dt.strftime("%H:%M")
    st.dataframe(first_events[["ФИО", "Роль", "Время", "Статус"]], use_container_width=True, hide_index=True, column_config={"Статус": st.column_config.TextColumn(width="small")})


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
        finally:
            session.close()
        status_names = {
            "ready": "Готов",
            "pending": "Обрабатывается",
            "invalid": "Требуется фото",
        }
        ready_count = sum(person.embedding_status == "ready" for person in people)
        first, second, third = st.columns(3)
        first.metric("Сотрудники ERP", len(people))
        second.metric("Локальные сотрудники", len(local_employees))
        third.metric("ERP готовы к распознаванию", ready_count)
        st.subheader("Каталог ERP")
        if people:
            st.caption("Основной каталог поступает из ERP. Дополнительные локальные фото добавляются на вкладке «Регистрация» и не изменяют ERP.")
            st.dataframe(
                pd.DataFrame([{
                    "ФИО": display_name(person.fio or f"{person.person_type} {person.id[:8]}"),
                    "Тип": person.person_type,
                    "Распознавание": status_names.get(person.embedding_status, person.embedding_status),
                    "Доп. фото": photo_counts.get(person.id, 0),
                } for person in people]),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("Каталог ERP ещё не загружен в локальный кэш.")
        st.markdown("<hr class='section-rule'>", unsafe_allow_html=True)
        st.subheader("Локальная база")
        if local_employees:
            st.dataframe(
                pd.DataFrame([{
                    "ФИО": display_name(employee.full_name),
                    "Роль": employee.role or "Не указана",
                    "Шаблон": "Готов" if employee.face_embeddings else "Нет",
                    "Локальных посещений": local_event_counts.get(employee.id, 0),
                } for employee in local_employees]),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("Локальных сотрудников пока нет. Их можно добавить на вкладке «Регистрация».")
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
        st.dataframe(pd.DataFrame([{"Дата": log.timestamp.strftime("%d.%m.%Y"), "Время": log.timestamp.strftime("%H:%M:%S"), "Событие": log.event_type} for log in logs]), use_container_width=True, hide_index=True)
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
    metrics = st.columns(5)
    metrics[0].metric("Активные люди", active_people)
    metrics[1].metric("С embeddings", embeddings)
    metrics[2].metric("Доп. фото", local_photo_count)
    metrics[3].metric("В очереди", queued)
    metrics[4].metric("Требуют внимания", failed)
    if sync_state and sync_state.last_error:
        st.warning(f"Последняя ошибка синхронизации: {sync_state.last_error}")
    elif sync_state and sync_state.last_incremental_sync_at:
        st.success(f"Последняя синхронизация: {sync_state.last_incremental_sync_at}")
    else:
        st.info("Синхронизация начнётся при запуске edge-sync.")

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
        with st.container(border=True):
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
