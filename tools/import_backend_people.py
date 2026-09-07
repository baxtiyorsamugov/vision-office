"""Import an approved ERP catalog into the local recognition cache.

This is an operator-run recovery/bootstrap tool for ERP deployments whose
device sync endpoint does not yet expose names and source-photo URLs.  It does
not save an administrator password, access token, or downloaded source list.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.edge.config import load_edge_settings
from core.edge.service import EdgeService, PERSON_TYPES
from database.models import RemotePerson


def request_json(url: str, method: str = "GET", token: str | None = None, form: dict[str, str] | None = None) -> Any:
    headers = {"Accept": "application/json"}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        body = urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"ERP request failed with HTTP {error.code}") from error
    except (URLError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"ERP request failed: {error}") from error


def login(base_url: str, username: str, password: str) -> str:
    payload = request_json(
        f"{base_url}/api/v1/users/login",
        method="POST",
        form={"username": username, "password": password},
    )
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("ERP login response did not include access_token")
    return token


def fetch_people(base_url: str, center_id: str, token: str) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    offset = 0
    limit = 200
    while True:
        query = urlencode({"limit": limit, "offset": offset})
        payload = request_json(
            f"{base_url}/api/v1/learning-centers/{center_id}/persons?{query}",
            token=token,
        )
        if not isinstance(payload, list):
            raise RuntimeError("ERP people response must be a JSON list")
        page = [item for item in payload if isinstance(item, dict)]
        people.extend(page)
        if len(page) < limit:
            return people
        offset += limit


def import_people(people: list[dict[str, Any]], photo_delay_seconds: float) -> tuple[int, int, int]:
    settings = load_edge_settings(PROJECT_ROOT / "config" / "settings.yaml")
    if not settings.configured:
        raise RuntimeError("edge_integration must be configured with a device API key before import")
    service = EdgeService(settings)
    imported = ready = failed = 0
    for item in people:
        person_id = str(item.get("id") or "")
        person_type = str(item.get("person_type") or "")
        if not person_id or person_type not in PERSON_TYPES:
            failed += 1
            continue
        session = service.Session()
        try:
            service._upsert_person(session, {
                "id": person_id,
                "person_type": person_type,
                "fio": item.get("fio"),
                "person_photo_url": item.get("photo_url"),
                "active": bool(item.get("active", True)),
                # The person-detail API has no embedding; Edge derives it from
                # the official source photo and stores only the local vector.
                "embedding": None,
            })
            session.commit()
            imported += 1
            person = session.get(RemotePerson, person_id)
            if person is not None and person.embedding_status == "ready":
                ready += 1
        except Exception:
            session.rollback()
            failed += 1
        finally:
            session.close()
        if photo_delay_seconds:
            time.sleep(photo_delay_seconds)
    return imported, ready, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Import ERP people into the local Vision Office recognition cache.")
    parser.add_argument("--base-url", required=True, help="ERP HTTPS base URL")
    parser.add_argument("--center-id", required=True, help="Learning Center UUID")
    parser.add_argument("--username", required=True, help="Temporary ERP administrator username")
    parser.add_argument("--password-env", default="VISION_OFFICE_IMPORT_PASSWORD", help="Environment variable containing the temporary password")
    parser.add_argument("--photo-delay-seconds", type=float, default=0.35, help="Pause between photos to protect the running worker")
    args = parser.parse_args()
    if args.photo_delay_seconds < 0:
        parser.error("--photo-delay-seconds must be zero or greater")
    password = os.environ.get(args.password_env)
    if not password:
        parser.error(f"Set {args.password_env} for this process; the password is never written to disk")

    base_url = args.base_url.rstrip("/")
    token = login(base_url, args.username, password)
    people = fetch_people(base_url, args.center_id, token)
    imported, ready, failed = import_people(people, args.photo_delay_seconds)
    print(f"ERP catalog: {len(people)}; local records updated: {imported}; recognition-ready: {ready}; failed: {failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
