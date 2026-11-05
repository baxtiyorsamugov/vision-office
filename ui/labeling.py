"""«Разметка» page: staff confirm who an unknown or almost-recognised camera face is."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from core.face_labeling import FaceLabelingService, PROJECT_ROOT

DECISION_LABELS = {
    "added": "Добавлен эталон", "forced": "Добавлен админом", "rejected": "Отклонён автопроверкой",
    "closed": "Отказ подтверждён", "skip": "Пропущен", "bad": "Плохой кадр", "undone": "Эталон выключен",
}
PAGE_SIZE = 8


_SERVICE: FaceLabelingService | None = None


@st.cache_resource
def _service(_engine, threshold: float) -> FaceLabelingService:
    return FaceLabelingService(_engine, recognition_threshold=threshold)


@st.cache_data(ttl=300, show_spinner="Ищу кадры для разметки…")
def _queue(threshold: float, days: int, camera, near_miss_only: bool, version: int):
    # `version` changes after every decision, so a click never shows a stale card.
    return _SERVICE.queue(days=days, camera_id=camera, near_miss_only=near_miss_only, limit=60)


@st.cache_data(ttl=600)
def _people(version: int):
    return _SERVICE.people()


@st.cache_data(ttl=300)
def _cameras():
    return _SERVICE.cameras()


def _bump() -> None:
    st.session_state["labeling_version"] = st.session_state.get("labeling_version", 0) + 1


def _abs(path: str) -> str:
    return str(PROJECT_ROOT / path)


def _flash(kind: str, text: str) -> None:
    st.session_state["labeling_flash"] = (kind, text)
    _bump()


def _person_block(service, person_id, title):
    """Main EduSchool photo first, then the camera frames already confirmed for this person."""
    main = service.main_photo(person_id)
    camera = service.camera_photos(person_id, limit=2)
    images, captions = [], []
    if main:
        images.append(_abs(main)); captions.append("основное фото")
    for path in camera:
        images.append(_abs(path)); captions.append("с камеры")
    if images:
        st.image(images, width=95, caption=captions)
    else:
        st.caption("У человека нет ни одного фото.")
    if not main:
        st.caption("Основного фото из EduSchool нет.")


def _render_card(service, item, people, labels, actor, index):
    with st.container(border=True):
        frames, guess, actions = st.columns([1.25, 1.25, 1.3], gap="medium")
        with frames:
            when = pd.Timestamp(item.created_at).tz_convert("Asia/Tashkent").strftime("%d.%m %H:%M") if item.created_at.tzinfo else str(item.created_at)[:16]
            st.caption(f"Камера **{item.camera_id}** · {when} · кадров в проходе: {len(item.group_event_ids)}")
            st.image([_abs(p) for p in item.group_photos[:3]], width=110)
        with guess:
            if item.best:
                st.markdown(f"Система думает: **{item.best.name}**  \nсходство {item.best.score:.2f} (нужно > {service.threshold:.2f})")
                _person_block(service, item.best.person_id, item.best.name)
                if item.second:
                    st.caption(f"Второй вариант: {item.second.name} · {item.second.score:.2f}")
            else:
                st.markdown("Система не знает, кто это.")
        with actions:
            key = f"lab_{item.event_id}"
            if item.best and st.button(f"Да, это {item.best.name.split(' ')[0]}", key=f"{key}_yes", type="primary",
                                       width="stretch", disabled=not actor):
                decision, message = service.confirm(item.event_id, item.best.person_id, actor, group_event_ids=item.group_event_ids)
                _flash("success" if decision == "added" else "warning", f"{item.best.name}: {message}")
                st.rerun()
            chosen = st.selectbox("Другой человек", people, index=None, key=f"{key}_pick", format_func=labels.get,
                                  placeholder="Начните вводить фамилию", label_visibility="collapsed")
            if chosen:
                _person_block(service, chosen, labels.get(chosen, ""))
            if st.button("Сохранить выбранного", key=f"{key}_save", width="stretch", disabled=not (actor and chosen)):
                decision, message = service.confirm(item.event_id, chosen, actor, group_event_ids=item.group_event_ids)
                _flash("success" if decision == "added" else "warning", f"{labels[chosen]}: {message}")
                st.rerun()
            skip, bad = st.columns(2)
            if skip.button("Не знаю", key=f"{key}_skip", width="stretch", disabled=not actor):
                service.dismiss(item.group_event_ids, actor, "skip")
                _bump()
                st.rerun()
            if bad.button("Плохой кадр", key=f"{key}_bad", width="stretch", disabled=not actor):
                service.dismiss(item.group_event_ids, actor, "bad")
                _bump()
                st.rerun()


def render_labeling(engine, render_header) -> None:
    from core.eduschool.catalog import load_settings

    global _SERVICE
    threshold = float(load_settings().recognition_threshold)
    service = _SERVICE = _service(engine, threshold)
    version = st.session_state.get("labeling_version", 0)
    render_header("Разметка лиц", "Подтвердите, кто на кадре, — подтверждённые кадры становятся эталонами")
    flash = st.session_state.pop("labeling_flash", None)
    if flash:
        (st.success if flash[0] == "success" else st.warning)(flash[1])

    actor = st.text_input("Кто размечает", key="labeling_actor", placeholder="Ваше имя, например «Дилноза (5-А)»").strip()
    if not actor:
        st.info("Введите своё имя — оно сохраняется в журнале у каждого решения.")
    queue_tab, admin_tab, journal_tab = st.tabs(["Очередь", "Нужна проверка админа", "Журнал"])

    with queue_tab:
        f1, f2, f3 = st.columns([1, 1.3, 2])
        days = f1.selectbox("Период", [1, 3, 7, 14], index=1, format_func=lambda d: f"{d} дн.")
        cameras = _cameras()
        camera = f2.selectbox("Камера", [None] + cameras, format_func=lambda c: "Все камеры" if c is None else c)
        mode = f3.radio("Показать", ["Почти узнанные", "Все неизвестные с подсказкой"], horizontal=True)
        st.caption("Кадры сверху, мелкие и размытые не показываются: такие эталоны путают систему. "
                   "Эталон включается сразу, если кадр похож на этого человека больше, чем на других; иначе уходит админу.")
        items = _queue(threshold, days, camera, mode == "Почти узнанные", version)
        if not items:
            st.success("Очередь пуста. Новые кадры появятся по мере проходов.")
        else:
            people_rows = _people(version)
            people = [pid for pid, _ in people_rows]
            labels = dict(people_rows)
            pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = st.selectbox("Страница", range(1, pages + 1), format_func=lambda v: f"{v} / {pages}",
                                key="labeling_page") if pages > 1 else 1
            st.caption(f"В очереди: {len(items)}")
            for index, item in enumerate(items[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]):
                _render_card(service, item, people, labels, actor, index)

    with admin_tab:
        st.caption("Сюда попадают кадры, которые автопроверка не пустила: они больше похожи на другого человека "
                   "или мало похожи на свои фото. Добавляйте, только если уверены.")
        rejected = service.journal(limit=50, decisions=["rejected"])
        if not rejected:
            st.success("Нет кадров на проверке.")
        for decision, name, photo in rejected:
            with st.container(border=True):
                left, mid, right = st.columns([1, 2, 1.2])
                left.image(_abs(photo), width=110)
                mid.markdown(f"**{name or '—'}** · отметил(а) {decision.actor}  \n{decision.detail or ''}")
                if decision.person_id:
                    with mid:
                        _person_block(service, decision.person_id, name or "")
                if right.button("Всё равно добавить", key=f"force_{decision.id}", disabled=not actor, width="stretch"):
                    result, message = service.confirm(decision.event_id, decision.person_id, actor, force=True)
                    _flash("success" if result == "added" else "warning", message)
                    st.rerun()
                if right.button("Согласен, не добавлять", key=f"close_{decision.id}", disabled=not actor, width="stretch"):
                    service.close_rejected(decision.id, actor)
                    _bump()
                    st.rerun()

    with journal_tab:
        rows = service.journal(limit=200)
        if not rows:
            st.caption("Пока нет решений.")
        else:
            st.dataframe(pd.DataFrame([{
                "Время": pd.Timestamp(d.created_at).tz_convert("Asia/Tashkent").strftime("%d.%m %H:%M") if d.created_at.tzinfo else str(d.created_at)[:16],
                "Кто": d.actor, "Решение": DECISION_LABELS.get(d.decision, d.decision), "Человек": name or "",
                "Сходство со своими": None if d.own_score is None else round(d.own_score, 2),
                "С другим": None if d.other_score is None else round(d.other_score, 2),
            } for d, name, _ in rows]), width="stretch", hide_index=True)
            added = [(d, name) for d, name, _ in rows if d.decision in {"added", "forced"}]
            if added:
                st.markdown("**Выключить ошибочный эталон**")
                pick = st.selectbox("Решение", [d.id for d, _ in added], index=None,
                                    format_func=lambda i: next(f"{n} · {d.actor} · {str(d.created_at)[:16]}" for d, n in added if d.id == i),
                                    placeholder="Выберите добавленный эталон", label_visibility="collapsed")
                if st.button("Выключить эталон", disabled=not (actor and pick)):
                    _flash("success", service.undo(pick, actor))
                    st.rerun()
