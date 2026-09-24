"""Operator-approved, device-local reference photos for EduSchool identities."""

from __future__ import annotations

import hashlib
import math
import uuid
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from database.manager import get_engine
from database.migrations import run_migrations
from database.models import Employee, EduSchoolCatalogPerson, EduSchoolReferencePhoto, RemotePerson, RemotePersonReferencePhoto


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000


def recognition_id(person: EduSchoolCatalogPerson) -> str:
    kind = {"employee": "e", "student": "s"}.get(person.person_type)
    if not kind or len(person.external_id) != 24:
        raise ValueError("Invalid EduSchool person identity")
    return f"edu:{kind}:{person.external_id}"


def normalized_embedding(value) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float32)
    if vector.shape != (512,) or not np.isfinite(vector).all():
        raise ValueError("A finite 512-value FaceID embedding is required")
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("FaceID embedding has no usable norm")
    return vector / norm


def load_recognition_embeddings(session) -> tuple[list[dict[str, str]], list[np.ndarray]]:
    rows = session.query(EduSchoolReferencePhoto, EduSchoolCatalogPerson).join(
        EduSchoolCatalogPerson, EduSchoolReferencePhoto.person_id == EduSchoolCatalogPerson.id
    ).filter(
        EduSchoolReferencePhoto.active.is_(True), EduSchoolCatalogPerson.active.is_(True)
    ).all()
    identities = []
    vectors = []
    for photo, person in rows:
        try:
            vector = normalized_embedding(photo.embedding)
            person_id = recognition_id(person)
        except (ValueError, TypeError, OverflowError):
            continue
        identities.append({
            "name": person.full_name,
            "person_id": person_id,
            "person_type": f"eduschool_{person.person_type}",
        })
        vectors.append(vector)
    return identities, vectors


class EduSchoolPhotoService:
    def __init__(self, engine=None, *, reference_limit: int = 10, root: Path = PROJECT_ROOT):
        self.engine = engine if engine is not None else get_engine()
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.reference_limit = max(1, min(30, int(reference_limit)))
        self.root = Path(root)

    def _reject_existing_identity(self, vector: np.ndarray, person_id: str) -> None:
        with self.Session() as session:
            legacy_vectors = [person.embedding for person in session.query(RemotePerson).filter(
                RemotePerson.active.is_(True), RemotePerson.embedding.is_not(None)
            ).all()]
            legacy_vectors.extend(photo.embedding for photo in session.query(RemotePersonReferencePhoto).join(
                RemotePerson, RemotePersonReferencePhoto.person_id == RemotePerson.id
            ).filter(RemotePerson.active.is_(True), RemotePersonReferencePhoto.active.is_(True)).all())
            for employee in session.query(Employee).all():
                legacy_vectors.extend(employee.face_embeddings or [])
            legacy_vectors.extend(photo.embedding for photo in session.query(EduSchoolReferencePhoto).join(
                EduSchoolCatalogPerson, EduSchoolReferencePhoto.person_id == EduSchoolCatalogPerson.id
            ).filter(
                EduSchoolReferencePhoto.person_id != person_id,
                EduSchoolReferencePhoto.active.is_(True),
                EduSchoolCatalogPerson.active.is_(True),
            ).all())
        for stored in legacy_vectors:
            try:
                similarity = float(np.dot(vector, normalized_embedding(stored)))
            except (ValueError, TypeError, OverflowError):
                continue
            if similarity >= 0.55:
                raise ValueError("Похожее лицо уже есть в основном или локальном каталоге. Добавьте фото к существующему профилю.")

    def add_local_photo(
        self,
        person_id: str,
        raw_photo: bytes,
        embedding_fn: Callable[[np.ndarray], object],
    ) -> EduSchoolReferencePhoto:
        if not raw_photo or len(raw_photo) > MAX_PHOTO_BYTES:
            raise ValueError("Фото должно быть не пустым и не больше 10 МБ.")
        checksum = hashlib.sha256(raw_photo).hexdigest()
        with self.Session() as session:
            person = session.get(EduSchoolCatalogPerson, person_id)
            if person is None or not person.active:
                raise ValueError("Выберите активного человека из каталога EduSchool.")
            if session.query(EduSchoolReferencePhoto).filter_by(person_id=person_id, image_checksum=checksum).first():
                raise ValueError("Это фото уже добавлено к профилю.")
            if session.query(EduSchoolReferencePhoto).filter_by(person_id=person_id, active=True, source="local").count() >= self.reference_limit:
                raise ValueError(f"Достигнут лимит: {self.reference_limit} фото на человека.")

        image = cv2.imdecode(np.frombuffer(raw_photo, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.size == 0 or image.shape[0] * image.shape[1] > MAX_IMAGE_PIXELS:
            raise ValueError("Фото повреждено или имеет слишком большое разрешение.")
        embedding = embedding_fn(image)
        if embedding is None:
            raise ValueError("На фото не найдено пригодное для распознавания лицо.")
        try:
            vector = normalized_embedding(embedding)
        except ValueError as error:
            raise ValueError("На фото не найдено пригодное для распознавания лицо.") from error
        self._reject_existing_identity(vector, person_id)

        record_id = str(uuid.uuid4())
        folder = self.root / "data" / "persons" / "eduschool" / hashlib.sha256(person_id.encode()).hexdigest()[:20]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{checksum[:16]}-{record_id[:8]}.jpg"
        if not cv2.imwrite(str(path), image):
            raise ValueError("Не удалось сохранить локальное фото.")
        try:
            with self.Session.begin() as session:
                person = session.get(EduSchoolCatalogPerson, person_id)
                if person is None or not person.active:
                    raise ValueError("Профиль больше не активен в EduSchool.")
                if session.query(EduSchoolReferencePhoto).filter_by(person_id=person_id, image_checksum=checksum).first():
                    raise ValueError("Это фото уже добавлено к профилю.")
                if session.query(EduSchoolReferencePhoto).filter_by(person_id=person_id, active=True, source="local").count() >= self.reference_limit:
                    raise ValueError(f"Достигнут лимит: {self.reference_limit} фото на человека.")
                record = EduSchoolReferencePhoto(
                    id=record_id,
                    person_id=person_id,
                    photo_path=str(path.relative_to(self.root)).replace("\\", "/"),
                    image_checksum=checksum,
                    embedding=vector.tolist(),
                    active=True,
                )
                session.add(record)
            return record
        except IntegrityError as error:
            path.unlink(missing_ok=True)
            raise ValueError("Это фото уже добавлено к профилю.") from error
        except Exception:
            path.unlink(missing_ok=True)
            raise
