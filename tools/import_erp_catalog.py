"""Interactive, device-scoped ERP catalog bootstrap. Credentials stay in memory."""
from __future__ import annotations

import getpass
import sys
import time
import warnings
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2
import numpy as np
import requests

from core.edge.config import load_edge_settings
from core.edge.service import EdgeService
from database.models import RemotePerson
from tools.import_backend_people import fetch_people, login


class CatalogService(EdgeService):
    token = ""

    def _download_reference_photo(self, person_id, photo_url):
        url = urljoin(self.settings.base_url + "/", photo_url)
        origin = urlsplit(self.settings.base_url)
        for _ in range(5):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.username or parsed.password:
                raise ValueError("Photo URL must use HTTPS without credentials")
            same_origin = (parsed.hostname, parsed.port) == (origin.hostname, origin.port)
            headers = {"Authorization": f"Bearer {self.token}"} if same_origin else {}
            with requests.get(url, headers=headers, timeout=30, stream=True, allow_redirects=False) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["Location"])
                    continue
                if response.status_code != 200:
                    raise ValueError(f"Photo download HTTP {response.status_code}")
                raw = bytearray()
                for chunk in response.iter_content(65536):
                    raw.extend(chunk)
                    if len(raw) > 10 * 1024 * 1024:
                        raise ValueError("Photo exceeds 10 MB")
            image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("Photo is not a valid image")
            target = ROOT / "data" / "persons" / f"{UUID(person_id)}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(target), image):
                raise ValueError("Cannot save photo")
            return image, target.relative_to(ROOT).as_posix()
        raise ValueError("Too many photo redirects")


def import_catalog(service, people, device_people):
    allowed = {item["id"]: item for item in device_people}
    # Fetch and validate the entire selection before touching local records.
    selected = [item for item in people if item.get("id") in allowed]
    if people and not selected:
        raise ValueError("This center has no employees in the device catalog. Check Center UUID.")
    for item in selected:
        UUID(item["id"])
        if not str(item.get("fio") or "").strip():
            raise ValueError("ERP catalog contains an employee without fio")
    updated = ready = photos = errors = 0
    for index, item in enumerate(selected, 1):
        with service.Session() as session:
            try:
                payload = {**item, "active": allowed[item["id"]].get("active", True),
                           "embedding": allowed[item["id"]].get("embedding")}
                service._upsert_person(session, payload)
                person = session.get(RemotePerson, item["id"])
                # Existing vectors must not prevent downloading a missing portrait.
                if person.photo_url and not (ROOT / (person.photo_path or "")).is_file():
                    _, person.photo_path = service._download_reference_photo(person.id, person.photo_url)
                session.commit()
                updated += 1
                ready += person.embedding_status == "ready"
                photos += bool(person.photo_path and (ROOT / person.photo_path).is_file())
                if person.embedding_status != "ready":
                    errors += 1
                    print(f"[{index}/{len(selected)}] FaceID unavailable; check reference photo.", flush=True)
                else:
                    print(f"[{index}/{len(selected)}] OK", flush=True)
            except Exception as error:
                session.rollback()
                errors += 1
                print(f"[{index}/{len(selected)}] Failed ({type(error).__name__}); retry import.", flush=True)
        time.sleep(0.35)
    print(f"Updated: {updated}; photos: {photos}; FaceID ready: {ready}; issues: {errors}; skipped: {len(people)-len(selected)}")
    return 2 if errors else 0


def main():
    if "--help" in sys.argv:
        print("Interactive ERP employee import. Reads config/settings.yaml; prompts for center UUID, username and hidden password.")
        return 0
    service = None
    try:
        settings = load_edge_settings(ROOT / "config/settings.yaml")
        if not settings.configured or settings.validation_error():
            raise ValueError("Configure edge_integration in config/settings.yaml first")
        if urlsplit(settings.base_url).scheme != "https":
            raise ValueError("ERP login requires HTTPS")
        print("One-time ERP employee import. Center UUID is NOT the device UUID.")
        center = str(UUID(input("Learning center UUID: ").strip()))
        username = input("ERP username: ").strip()
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("ERP password: ")
        try:
            token = login(settings.base_url, username, password)
        finally:
            password = None
        people = fetch_people(settings.base_url, center, token)
        service = CatalogService(settings)
        service.token = token
        device_people = service._fetch_people(None)
        return import_catalog(service, people, device_people)
    except (KeyboardInterrupt, EOFError):
        print("Import cancelled.")
        return 130
    except Exception as error:
        # Do not print response bodies, URLs, tokens or credential tracebacks.
        if isinstance(error, (ValueError, RuntimeError)):
            print(f"Import stopped: {error}")
        else:
            print(f"Import stopped ({type(error).__name__}). Check ERP access and PostgreSQL.")
        return 1
    finally:
        if service:
            service.token = ""
            service.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
