"""Copy the scoped EduSchool directory into a separate local PostgreSQL cache."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json

import yaml
from sqlalchemy.orm import sessionmaker

from database.manager import get_engine
from database.migrations import run_migrations
from database.models import EduSchoolCatalogPerson, EduSchoolCatalogSyncState, EduSchoolReferencePhoto


logger = logging.getLogger("vision_office.eduschool")
SOURCE_ID = re.compile(r"^[0-9a-fA-F]{24}$")


@dataclass(frozen=True)
class EduSchoolCatalogSettings:
    enabled: bool = False
    base_url: str = "https://backend.eduschool.uz"
    branch_id: str = ""
    bearer_token: str = ""
    api_key: str = ""
    sync_interval_seconds: int = 3600
    page_size: int = 200
    request_timeout_seconds: int = 20
    local_reference_photo_limit: int = 10
    recognition_threshold: float = 0.55

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.branch_id and self.bearer_token and self.api_key)


def load_settings(path: str | Path = "config/settings.yaml") -> EduSchoolCatalogSettings:
    config_path = Path(path)
    values = {}
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as source:
            values = (yaml.safe_load(source) or {}).get("eduschool_catalog") or {}
    defaults = EduSchoolCatalogSettings()
    return EduSchoolCatalogSettings(
        enabled=bool(values.get("enabled", defaults.enabled)),
        base_url=str(values.get("base_url", defaults.base_url)).rstrip("/"),
        branch_id=str(values.get("branch_id", defaults.branch_id)).strip(),
        bearer_token=os.getenv("EDUSCHOOL_EXTERNAL_BEARER_TOKEN", "").strip(),
        api_key=os.getenv("EDUSCHOOL_EXTERNAL_API_KEY", "").strip(),
        sync_interval_seconds=max(60, int(values.get("sync_interval_seconds", defaults.sync_interval_seconds))),
        page_size=min(500, max(1, int(values.get("page_size", defaults.page_size)))),
        request_timeout_seconds=max(1, int(values.get("request_timeout_seconds", defaults.request_timeout_seconds))),
        local_reference_photo_limit=min(30, max(1, int(values.get("local_reference_photo_limit", defaults.local_reference_photo_limit)))),
        recognition_threshold=min(0.9, max(0.4, float(values.get("recognition_threshold", defaults.recognition_threshold)))),
    )


class EduSchoolCatalogSync:
    def __init__(self, settings: EduSchoolCatalogSettings, engine=None):
        self.settings = settings
        self.engine = engine or get_engine()
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def _fetch_page(self, person_type: str, page: int) -> dict:
        if not self.settings.base_url.startswith("https://"):
            raise ValueError("EduSchool External API requires an HTTPS base URL")
        query = urlencode({"page": page, "limit": self.settings.page_size})
        url = f"{self.settings.base_url}/external-api/{person_type}s/pagin?{query}"
        request = Request(url, headers={
            "Authorization": f"Bearer {self.settings.bearer_token}",
            "apiKey": self.settings.api_key,
            "branch": self.settings.branch_id,
            "Accept": "application/json",
        })
        try:
            with urlopen(request, timeout=self.settings.request_timeout_seconds) as response:
                return json.load(response)
        except HTTPError as error:
            raise ValueError(f"EduSchool {person_type} page {page}: HTTP {error.code}") from error
        except (URLError, OSError, ValueError) as error:
            raise ValueError(f"EduSchool {person_type} page {page}: request failed") from error

    def _fetch_all(self, person_type: str) -> list[dict]:
        result: list[dict] = []
        page = 1
        expected_total: int | None = None
        while True:
            payload = self._fetch_page(person_type, page)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise ValueError(f"EduSchool {person_type}: unexpected response shape")
            total = data.get("total")
            if not isinstance(total, int) or total < 0:
                raise ValueError(f"EduSchool {person_type}: invalid total")
            if expected_total is None:
                expected_total = total
            rows = data["data"]
            if len(rows) > self.settings.page_size or (not rows and len(result) < expected_total):
                raise ValueError(f"EduSchool {person_type}: incomplete page {page}")
            result.extend(rows)
            if len(result) >= expected_total:
                if len(result) != expected_total:
                    raise ValueError(f"EduSchool {person_type}: page count changed during sync")
                return result
            page += 1
            if page > 10000:
                raise ValueError(f"EduSchool {person_type}: pagination limit exceeded")

    def _normalize(self, person_type: str, row: dict) -> dict:
        if not isinstance(row, dict) or not SOURCE_ID.fullmatch(str(row.get("_id") or "")):
            raise ValueError(f"EduSchool {person_type}: invalid external ID")
        branch = row.get("branchId") if person_type == "student" else (row.get("branchEmployee") or {}).get("branchId")
        if str(branch) != self.settings.branch_id:
            raise ValueError(f"EduSchool {person_type}: record outside configured branch")
        raw_status = row.get("status") if person_type == "student" else row.get("state")
        status = raw_status.get("state") if isinstance(raw_status, dict) else raw_status
        status = str(status or "unknown")[:40]
        active = status == "active"
        if person_type == "employee":
            active = active and (row.get("branchEmployee") or {}).get("isActive") is True
        name = str(row.get("fullName") or " ".join(str(row.get(field) or "").strip() for field in ("firstName", "lastName", "middleName"))).strip()
        if not name:
            raise ValueError(f"EduSchool {person_type}: record without a name")
        image_url = str(row.get("imageUrl") or "").strip()
        employee_no = str(row.get("employeeNo") or "").strip() if person_type == "employee" else ""
        return {
            "id": f"{person_type}:{row['_id']}",
            "person_type": person_type,
            "external_id": row["_id"],
            "full_name": name[:255],
            "image_url": image_url[:1024] if image_url else None,
            "employee_no": employee_no if 1 <= len(employee_no) <= 64 else None,
            "source_status": status,
            "active": active,
        }

    def sync_once(self) -> dict[str, int]:
        if not self.settings.configured:
            raise ValueError("EduSchool catalog credentials or branch are missing")
        try:
            snapshots = {}
            for kind in ("student", "employee"):
                source_rows = self._fetch_all(kind)
                unique_rows = {}
                original_rows = {}
                for source_row in source_rows:
                    row = self._normalize(kind, source_row)
                    if row["id"] in original_rows:
                        if source_row != original_rows[row["id"]]:
                            raise ValueError(f"EduSchool {kind}: conflicting duplicate ID")
                        continue
                    original_rows[row["id"]] = source_row
                    unique_rows[row["id"]] = row
                if len(source_rows) != len(unique_rows):
                    logger.warning(
                        "EduSchool %s catalog contained %s identical duplicate rows",
                        kind, len(source_rows) - len(unique_rows),
                    )
                snapshots[kind] = list(unique_rows.values())
            all_rows = snapshots["student"] + snapshots["employee"]
            ids = [row["id"] for row in all_rows]
            id_set = set(ids)
            if len(id_set) != len(ids):
                raise ValueError("EduSchool catalog contains duplicate IDs")
            now = datetime.now(timezone.utc)
            with self.Session.begin() as session:
                existing = {person.id: person for person in session.query(EduSchoolCatalogPerson).all()}
                for row in all_rows:
                    person = existing.get(row["id"])
                    if person is None:
                        person = EduSchoolCatalogPerson(id=row["id"])
                        session.add(person)
                    elif person.image_url != row["image_url"]:
                        session.query(EduSchoolReferencePhoto).filter_by(person_id=person.id, source="eduschool").update(
                            {"active": False}, synchronize_session=False
                        )
                        person.source_photo_status = "pending" if row["image_url"] else "missing"
                        person.source_photo_error = None
                        person.source_photo_retry_at = None
                        person.attendance_approved = False
                        person.attendance_approved_at = None
                    if person.id in existing and person.employee_no != row["employee_no"]:
                        person.attendance_approved = False
                        person.attendance_approved_at = None
                    if not row["active"]:
                        person.attendance_approved = False
                        person.attendance_approved_at = None
                    if person.id not in existing:
                        person.source_photo_status = "pending" if row["image_url"] else "missing"
                    elif not row["image_url"] and person.source_photo_status != "missing":
                        person.source_photo_status = "missing"
                        person.source_photo_error = None
                        person.source_photo_retry_at = None
                    for field, value in row.items():
                        setattr(person, field, value)
                    person.last_seen_at = now
                for person in existing.values():
                    if person.id not in id_set:
                        person.active = False
                        person.source_status = "absent"
                        person.attendance_approved = False
                        person.attendance_approved_at = None
                state = session.get(EduSchoolCatalogSyncState, 1)
                if state is None:
                    state = EduSchoolCatalogSyncState(id=1)
                    session.add(state)
                state.last_success_at = now
                state.last_error = None
                state.student_count = len(snapshots["student"])
                state.employee_count = len(snapshots["employee"])
            logger.info("EduSchool catalog synced students=%s employees=%s", len(snapshots["student"]), len(snapshots["employee"]))
            return {"students": len(snapshots["student"]), "employees": len(snapshots["employee"])}
        except Exception as error:
            with self.Session.begin() as session:
                state = session.get(EduSchoolCatalogSyncState, 1)
                if state is None:
                    state = EduSchoolCatalogSyncState(id=1)
                    session.add(state)
                state.last_error = str(error)[:500]
            raise
