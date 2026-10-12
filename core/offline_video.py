"""Read-only gallery snapshots and deterministic offline review helpers."""

from __future__ import annotations

import json
import math
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def sample_due(frame_index, source_fps, sample_fps, next_sample):
    timestamp = frame_index / source_fps
    return timestamp + 1e-8 >= next_sample / sample_fps


def crop_bounds(box, width, height):
    x1, y1, x2, y2 = map(int, box)
    px, py = int((x2 - x1) * .20), int((y2 - y1) * .35)
    return max(0, x1 - px), max(0, y1 - py), min(width, x2 + px), min(height, y2 + py)


def latency_summary(values):
    if not values:
        return None
    ordered = sorted(values)

    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        lo, hi = math.floor(position), math.ceil(position)
        return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo), 1)

    return {"p50": percentile(.50), "p95": percentile(.95)}


@contextmanager
def readonly_session():
    """Use an existing database, with writes rejected by the database itself."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from database.manager import database_url

    url = database_url()
    if url.startswith("sqlite:///"):
        path = Path(url.removeprefix("sqlite:///")).resolve()
        if not path.is_file():
            raise FileNotFoundError("Existing database is required")
        url = f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true"
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            with connection.begin():
                if engine.dialect.name == "postgresql":
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                elif engine.dialect.name == "sqlite":
                    connection.execute(text("PRAGMA query_only=ON"))
                else:
                    raise ValueError("Only PostgreSQL and SQLite read-only snapshots are supported")
                yield sessionmaker(bind=connection)
    finally:
        engine.dispose()


def export_gallery(destination):
    """No service constructors, migrations or writes; DB enforces read-only access."""
    from core.ai.engine import _load_known_faces
    from core.edge.config import load_edge_settings
    from core.eduschool.catalog import load_settings
    from database.models import EduSchoolCatalogPerson

    with readonly_session() as Session:
        names, vectors = _load_known_faces(Session)
        with Session() as session:
            active = Counter(row.person_type for row in session.query(EduSchoolCatalogPerson).filter(
                EduSchoolCatalogPerson.active.is_(True)).all())
    unique = {item["person_id"]: item for item in names}
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "identities": names,
        "vectors": vectors.tolist() if vectors is not None else [],
        "threshold": load_edge_settings().recognition_threshold,
        "eduschool_threshold": load_settings().recognition_threshold,
        "summary": {"references": len(names), "people": len(unique),
                    "enrolled": dict(Counter(item["person_type"] for item in unique.values())),
                    "active_catalog": dict(active), "scope": "eduschool_and_local"},
    }
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload["summary"]


def write_report(report, destination, template):
    # JSON sits inside a script element: escape '<' even for application/json.
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    html = Path(template).read_text(encoding="utf-8").replace("__REPORT_JSON__", payload)
    Path(destination).write_text(html, encoding="utf-8")
