"""Camera controls and live diagnostics rendered without opening video streams."""
from dataclasses import replace
import json
import time
from uuid import uuid4

import pandas as pd
import streamlit as st
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from core.camera_controls import PROFILES, read_controls, save_camera
from core.config import CameraSettings, load_app_settings
from core.performance import PROJECT_ROOT, configured_camera_statuses
from core.preview import preview_path
from core.eduschool.turnstile import load_settings as load_turnstile_settings
from database.models import EduSchoolCatalogPerson, EduSchoolReferencePhoto, RemotePerson


STATE_LABELS = {"connected": "На связи", "reconnecting": "Переподключение", "stale": "Нет связи с worker",
                "waiting": "Ожидание worker", "disabled": "Выключена", "stopped": "Остановлена"}


@st.fragment(run_every="5s")
def render_runtime_panel(engine=None):
    try:
        cameras = configured_camera_statuses()
        delivery = load_turnstile_settings()
    except (ValueError, OSError):
        st.error("Не удалось прочитать настройки камер.")
        return
    try:
        path = PROJECT_ROOT / "data/supervisor_status.json"
        supervisor = json.loads(path.read_text(encoding="utf-8"))
        fresh = time.time() - path.stat().st_mtime < 15 and supervisor.get("running")
    except (OSError, ValueError, TypeError):
        supervisor, fresh = {}, False
    if not fresh:
        st.warning("Supervisor камер не на связи. Запустите сервис vision-worker; изменения применятся при его запуске.")
    elif supervisor.get("error"):
        st.error(supervisor["error"])
    resources = supervisor.get("resources", {}) if fresh else {}
    if resources:
        cols = st.columns(4)
        cols[0].metric("CPU", f"{resources.get('cpu_percent', 0):.0f}%")
        cols[1].metric("RAM", f"{resources.get('memory_percent', 0):.0f}%")
        cols[2].metric("Свободно RAM", f"{resources.get('memory_available_gb', 0)} ГБ")
        cols[3].metric("Свободно на диске", f"{resources.get('disk_free_gb', 0)} ГБ")
        st.caption("В Docker показатели CPU и RAM относятся к среде Linux / WSL, а не ко всей Windows.")
        for gpu in resources.get("gpus", []):
            st.caption(f"{gpu['name']} · GPU {gpu['utilization']}% · VRAM {gpu['memory_used_mb']} / {gpu['memory_total_mb']} МБ")
    if engine is not None:
        with Session(engine) as session:
            has_photo = session.query(EduSchoolReferencePhoto.id).filter(
                EduSchoolReferencePhoto.person_id == EduSchoolCatalogPerson.id,
                EduSchoolReferencePhoto.active.is_(True), EduSchoolReferencePhoto.embedding.isnot(None),
            ).exists()
            readiness = session.query(EduSchoolCatalogPerson.person_type, func.count(), func.sum(case((has_photo, 1), else_=0))).filter(
                EduSchoolCatalogPerson.active.is_(True)).group_by(EduSchoolCatalogPerson.person_type).all()
            legacy = session.query(RemotePerson).filter_by(active=True).count()
        for kind, total, ready in readiness:
            label = "Ученики" if kind == "student" else "Сотрудники"
            st.caption(f"{label} EduSchool · с фото FaceID: {ready or 0} из {total}")
        if legacy:
            st.info(f"Активных профилей старого ERP: {legacy}. При дублировании людей между каталогами проверьте, какой профиль распознаётся.")
    st.dataframe(pd.DataFrame([{
        "Камера": item["camera_name"], "Состояние": STATE_LABELS.get(item.get("stream_status"), "—"),
        "deviceId API": delivery.device_ids.get(item["camera_id"], "Не настроен"),
        "AI": "Готов" if item.get("ai_ready") and item.get("running") else "Не готов",
        "Захват FPS": item.get("capture_fps", 0), "Детекция FPS": item.get("detection_fps", 0),
        "YOLO": item.get("yolo_device", "—"), "FaceID": item.get("face_device", "—"),
        "YOLO p95, мс": item.get("detection_ms_p95"), "FaceID p95, мс": item.get("face_ms_p95"),
        "Очередь": item.get("face_queue_size", 0), "Ожидание, мс": item.get("face_queue_wait_ms"),
        "Эталоны": item.get("known_vectors", 0), "Возраст кадра, мс": item.get("frame_age_ms"),
        "Ошибки FaceID": item.get("face_errors", 0),
    } for item in cameras]).fillna("—").astype(str), hide_index=True, width="stretch")
    for item in cameras:
        if not item.get("enabled") or not item.get("running"):
            continue
        name = item["camera_name"]
        if item.get("ai_ready") and not item.get("known_vectors"):
            st.warning(f"{name}: нет эталонов FaceID. Проверьте фото и состояние профилей.")
        if (item.get("face_queue_wait_ms") or 0) > 500 or (item.get("detection_ms_p95") or 0) > 250:
            st.warning(f"{name}: высокая задержка обработки. Попробуйте экономичный профиль и проверьте доступность GPU.")
        if (item.get("frame_age_ms") or 0) > 2000:
            st.warning(f"{name}: кадры устарели. Проверьте сеть и поток камеры.")
        reason = item.get("yolo_fallback_reason") or item.get("face_fallback_reason")
        if reason:
            st.caption(f"{name}: {reason}")
        if item.get("cache_error"):
            st.warning(f"{name}: каталог FaceID не обновляется ({item['cache_error']}). Проверьте PostgreSQL.")


def render_cameras(engine=None):
    render_runtime_panel(engine)
    st.divider()
    try:
        _, revision = read_controls()
        settings = load_app_settings()
        delivery = load_turnstile_settings()
        if revision != read_controls()[1]:
            st.warning("Настройки обновились. Обновите страницу.")
            return
    except (OSError, ValueError):
        st.error("Не удалось загрузить конфигурацию камер.")
        return
    by_id = {camera.id: camera for camera in settings.cameras}
    if message := st.session_state.pop("camera_saved_message", None):
        st.success(message)
    if pending := st.session_state.pop("camera_pending_selection", None):
        st.session_state["camera_editor_selection"] = pending
    if st.button("Добавить камеру", icon=":material/add:"):
        st.session_state["camera_editor_selection"] = "__new__"
    if st.session_state.get("camera_editor_selection") not in ["__new__", *by_id]:
        st.session_state["camera_editor_selection"] = next(iter(by_id), "__new__")
    selected = st.selectbox("Камера", ["__new__", *by_id], index=None, key="camera_editor_selection",
                            format_func=lambda value: "Добавить камеру" if value == "__new__" else f"{by_id[value].name} · {value}")
    current = by_id.get(selected)
    device_id = delivery.device_ids.get(current.id, "") if current else ""
    new_id = st.session_state.setdefault("new_camera_id", f"camera_{uuid4().hex[:8]}")
    with st.form(f"camera-editor-{selected}"):
        left, right = st.columns(2)
        with left:
            camera_id = st.text_input("ID камеры", value=current.id if current else new_id, disabled=current is not None, max_chars=100)
            name = st.text_input("Название", value=current.name if current else "", max_chars=100)
            location = st.text_input("Расположение", value=current.location if current else "", max_chars=200)
            api_device = st.text_input("deviceId API", value=device_id, disabled=bool(device_id), max_chars=64,
                                       placeholder="По умолчанию — ID камеры")
        with right:
            source = st.text_input("Адрес потока RTSP", type="password", placeholder="Пустое поле сохраняет текущий адрес" if current else "rtsp://...", max_chars=2048)
            direction = st.selectbox("Направление", ["entry", "exit"], index=1 if current and current.event_type == "exit" else 0,
                                     format_func=lambda value: "Вход" if value == "entry" else "Выход")
            profile = st.selectbox("Профиль обработки", list(PROFILES),
                                  index=list(PROFILES).index(current.profile) if current else 1,
                                  format_func=PROFILES.get)
        enabled = st.toggle("Камера включена", value=current.is_active if current else False)
        submitted = st.form_submit_button("Сохранить камеру", type="primary", icon=":material/save:")
    if submitted:
        try:
            if current is None and camera_id.strip() in by_id:
                raise ValueError("Этот ID уже используется. Выберите существующую камеру для изменения.")
            camera = CameraSettings(id=camera_id.strip(), rtsp_url=source.strip() or (current.rtsp_url if current else ""),
                                    is_active=enabled, event_type=direction, name=name.strip(), location=location.strip(),
                                    profile=profile, restart_token=current.restart_token if current else 0,
                                    device_id=api_device.strip() or camera_id.strip())
            save_camera(camera, expected_revision=revision, base_device_ids=delivery.device_ids)
            st.session_state["camera_saved_message"] = "Камера и deviceId сохранены." + (" Запрошен запуск." if enabled else " Камера выключена.")
            st.session_state["camera_pending_selection"] = camera.id
            st.session_state.pop("new_camera_id", None)
            st.rerun()
        except (OSError, ValueError) as error:
            st.error(str(error) if isinstance(error, ValueError) else "Не удалось сохранить настройки на диск.")
    if current:
        if st.button("Перезапустить эту камеру", icon=":material/restart_alt:", disabled=not current.is_active):
            try:
                save_camera(replace(current, restart_token=current.restart_token + 1), expected_revision=revision,
                            base_device_ids=delivery.device_ids)
                st.session_state["camera_saved_message"] = "Запрос на перезапуск сохранён."
                st.rerun()
            except (OSError, ValueError):
                st.error("Обновите страницу и повторите запрос.")
        path = preview_path(current.id)
        if path.is_file():
            st.image(str(path), caption="Последний сохранённый кадр", width="stretch")
    if current:
        event_type = "check_out" if current.event_type == "exit" else "check_in"
        st.caption(f"EduSchool · deviceId: {device_id or 'Не настроен'} · eventType: {event_type}")
    if not delivery.enabled:
        st.warning("Отправка посещений EduSchool отключена в настройках этого ПК.")
    elif delivery.validation_error():
        st.warning("Настройки отправки EduSchool неполные. Проверьте раздел «Отправки».")
