"""Low-priority enrollment of EduSchool source images into local FaceID."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError
from sqlalchemy import or_

from core.eduschool.catalog import EduSchoolCatalogSettings
from core.eduschool.photos import EduSchoolPhotoService, MAX_IMAGE_PIXELS, MAX_PHOTO_BYTES, normalized_embedding
from database.models import EduSchoolCatalogPerson, EduSchoolReferencePhoto


logger = logging.getLogger("vision_office.eduschool.photos")
SOURCE_PATH = re.compile(r"^org-[0-9a-fA-F]{24}/uploads/[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png|webp)$", re.I)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class InvalidSourcePhoto(ValueError):
    pass


def source_photo_url(base_url: str, image_url: str) -> str:
    """Only fetch photos from the configured EduSchool backend, never an API-supplied host."""
    origin = urlsplit(base_url)
    parsed = urlsplit(image_url.strip())
    if parsed.scheme or parsed.netloc:
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            raise InvalidSourcePhoto("Photo host differs from the configured backend")
        path = parsed.path.removeprefix("/uploads/")
    else:
        path = image_url.strip().lstrip("/")
        path = path.removeprefix("uploads/")
    if (
        origin.scheme != "https" or not origin.hostname or
        parsed.query or parsed.fragment or not SOURCE_PATH.fullmatch(path) or
        any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise InvalidSourcePhoto("Invalid EduSchool image path")
    return f"{origin.scheme}://{origin.netloc}/uploads/{path}"


def download_source_photo(base_url: str, image_url: str, timeout: int) -> bytes:
    url = source_photo_url(base_url, image_url)
    request = Request(url, headers={"Accept": "image/jpeg,image/png,image/webp"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            if not (response.headers.get("Content-Type") or "").lower().startswith("image/"):
                raise InvalidSourcePhoto("Source did not return an image")
            if int(response.headers.get("Content-Length") or 0) > MAX_PHOTO_BYTES:
                raise InvalidSourcePhoto("Source image is larger than 10 MB")
            data = response.read(MAX_PHOTO_BYTES + 1)
    except (HTTPError, URLError, OSError) as error:
        raise ConnectionError(f"Source image request failed ({type(error).__name__})") from error
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise InvalidSourcePhoto("Source image is empty or larger than 10 MB")
    return data


def decode_source_photo(data: bytes) -> np.ndarray:
    try:
        with Image.open(io.BytesIO(data)) as photo:
            if photo.format not in {"JPEG", "PNG", "WEBP"} or photo.width * photo.height > MAX_IMAGE_PIXELS:
                raise InvalidSourcePhoto("Unsupported image format or dimensions")
            photo.verify()
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise InvalidSourcePhoto("Source image is damaged or unsupported") from error
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise InvalidSourcePhoto("Source image could not be decoded")
    return image


class EduSchoolAutoPhotoService:
    def __init__(self, settings: EduSchoolCatalogSettings, engine=None, *, root: Path | None = None):
        self.settings = settings
        self.photos = EduSchoolPhotoService(engine, root=root or Path(__file__).resolve().parents[2])
        self.Session = self.photos.Session

    def next_person_id(self) -> str | None:
        now = datetime.now(timezone.utc)
        with self.Session() as session:
            row = session.query(EduSchoolCatalogPerson.id).filter(
                EduSchoolCatalogPerson.active.is_(True),
                EduSchoolCatalogPerson.image_url.is_not(None),
                EduSchoolCatalogPerson.source_photo_status.in_(("pending", "failed")),
                or_(EduSchoolCatalogPerson.source_photo_retry_at.is_(None), EduSchoolCatalogPerson.source_photo_retry_at <= now),
            ).order_by(
                EduSchoolCatalogPerson.person_type.asc(), EduSchoolCatalogPerson.id.asc()
            ).first()
            return row[0] if row else None

    def process_one(self, person_id: str, embedding_fn) -> str:
        with self.Session() as session:
            person = session.get(EduSchoolCatalogPerson, person_id)
            if person is None or not person.active or not person.image_url:
                return "skipped"
            image_url = person.image_url
        try:
            raw = download_source_photo(self.settings.base_url, image_url, self.settings.request_timeout_seconds)
            image = decode_source_photo(raw)
            embedding = embedding_fn(image)
            if embedding is None:
                raise InvalidSourcePhoto("Exactly one usable face is required")
            try:
                vector = normalized_embedding(embedding)
            except ValueError as error:
                raise InvalidSourcePhoto("Invalid FaceID embedding") from error
            checksum = hashlib.sha256(raw).hexdigest()
            with self.Session() as session:
                existing = session.query(EduSchoolReferencePhoto).filter_by(person_id=person_id, image_checksum=checksum).first()
            if existing is not None and existing.source == "local" and not existing.active:
                raise InvalidSourcePhoto("Source matches a disabled local photo")
            if existing is None or not existing.active:
                try:
                    self.photos._reject_existing_identity(vector, person_id)
                except ValueError as error:
                    raise InvalidSourcePhoto("Face conflicts with another active profile") from error
            path = None
            if existing is None:
                folder = self.photos.root / "data" / "persons" / "eduschool" / hashlib.sha256(person_id.encode()).hexdigest()[:20]
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{checksum[:16]}-{uuid.uuid4().hex[:8]}.jpg"
                if not cv2.imwrite(str(path), image):
                    raise OSError("Could not save source image")
            committed = False
            try:
                with self.Session.begin() as session:
                    person = session.get(EduSchoolCatalogPerson, person_id)
                    if person is None or not person.active or person.image_url != image_url:
                        return "skipped"
                    if existing is None:
                        record = session.query(EduSchoolReferencePhoto).filter_by(person_id=person_id, image_checksum=checksum).first()
                        if record is None:
                            record = EduSchoolReferencePhoto(
                                id=str(uuid.uuid4()), person_id=person_id,
                                photo_path=str(path.relative_to(self.photos.root)).replace("\\", "/"),
                                image_checksum=checksum, embedding=vector.tolist(),
                                source="eduschool", source_url=image_url, active=True,
                            )
                            session.add(record)
                    elif existing.source == "eduschool":
                        record = session.get(EduSchoolReferencePhoto, existing.id)
                        record.source_url = image_url
                        record.active = True
                    person.source_photo_status = "ready"
                    person.source_photo_error = None
                    person.source_photo_retry_at = None
                committed = True
                logger.info("EduSchool source FaceID ready person_id=%s", person_id)
                return "ready"
            finally:
                if path is not None and not committed:
                    path.unlink(missing_ok=True)
        except InvalidSourcePhoto as error:
            status, reason, retry = "invalid", str(error), None
        except Exception as error:
            status, reason, retry = "failed", type(error).__name__, datetime.now(timezone.utc) + timedelta(hours=1)
        with self.Session.begin() as session:
            person = session.get(EduSchoolCatalogPerson, person_id)
            if person is not None and person.image_url == image_url:
                person.source_photo_status = status
                person.source_photo_error = reason[:255]
                person.source_photo_retry_at = retry
        logger.warning("EduSchool source FaceID %s person_id=%s reason=%s", status, person_id, reason)
        return status
