"""Bounded, read-only attendance queries over camera events."""
from datetime import datetime, timezone

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from core.local_time import as_utc, local_day_bounds_utc, to_local
from database.models import RecognitionEvent


PERSON_TYPES = ("eduschool_employee", "eduschool_student", "local_employee")
TYPE_LABELS = {"eduschool_employee": "Сотрудник EduSchool", "eduschool_student": "Ученик", "local_employee": "Локальный сотрудник"}


def utc_hour_expression(engine, column):
    timestamp = func.timezone("UTC", column) if engine.dialect.name == "postgresql" else column
    return func.extract("hour", timestamp)


def daily_analytics(engine, selected_date, *, person_type=None, search="", page=1, page_size=25):
    start, end = map(as_utc, local_day_bounds_utc(selected_date))
    page_size = max(1, min(100, int(page_size)))
    event = RecognitionEvent
    with Session(engine) as session:
        query = session.query(event).filter(
            event.created_at >= start, event.created_at < end,
            event.person_type.in_(PERSON_TYPES), event.person_id.isnot(None),
            event.event_type.in_(("entry", "exit")),
        )
        if person_type in PERSON_TYPES:
            query = query.filter(event.person_type == person_type)
        if search.strip():
            query = query.filter(event.person_name.ilike(f"%{search.strip()}%"))
        grouped = query.with_entities(
            event.person_type.label("person_type"), event.person_id.label("person_id"),
            func.max(event.person_name).label("name"),
            func.min(case((event.event_type == "entry", event.created_at))).label("first_entry"),
            func.max(case((event.event_type == "entry", event.created_at))).label("last_entry"),
            func.max(case((event.event_type == "exit", event.created_at))).label("last_exit"),
            func.sum(case((event.event_type == "entry", 1), else_=0)).label("entries"),
            func.sum(case((event.event_type == "exit", 1), else_=0)).label("exits"),
        ).group_by(event.person_type, event.person_id).subquery()
        totals = session.query(func.count(), func.sum(grouped.c.entries), func.sum(grouped.c.exits)).select_from(grouped).one()
        total = totals[0]
        page = max(1, min(int(page), max(1, (total + page_size - 1) // page_size)))
        rows = session.query(grouped).order_by(func.lower(grouped.c.name), grouped.c.person_type, grouped.c.person_id).offset((page - 1) * page_size).limit(page_size).all()
        hour = utc_hour_expression(engine, event.created_at)
        hours = query.with_entities(hour, event.event_type, func.count()).group_by(hour, event.event_type).all()
    hourly = [{"hour": hour, "entries": 0, "exits": 0} for hour in range(24)]
    for utc_hour, direction, count in hours:
        stamp = datetime(selected_date.year, selected_date.month, selected_date.day, int(utc_hour), tzinfo=timezone.utc)
        hourly[to_local(stamp).hour]["entries" if direction == "entry" else "exits"] += count
    return {"total": total, "entries": int(totals[1] or 0), "exits": int(totals[2] or 0),
            "page": page, "pages": max(1, (total + page_size - 1) // page_size),
            "rows": [dict(row._mapping) for row in rows], "hourly": hourly}
