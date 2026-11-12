"""Face-labeling page logic: staff confirm who an unknown camera face is.

Lessons from the 2026-11-04 replay review are built in:
* only usable frames are shown (size and sharpness gates);
* a confirmed frame becomes a reference ONLY if it is more similar to that
  person's existing photos than to anybody else's (28 bad references failed
  exactly this test and caused most false matches);
* every action is logged with its scores, and an added reference can be
  switched off again from the journal.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy.orm import sessionmaker

from core.eduschool.photos import EduSchoolPhotoService, normalized_embedding
from database.models import (EduSchoolCatalogPerson, EduSchoolReferencePhoto, FaceLabelDecision, RecognitionEvent,
                             UnknownFaceObservation)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIN_CROP_SIDE = 70          # padded crop; smaller faces gave scores ~0.35 and many detector misses
MIN_SHARPNESS = 25.0        # variance of Laplacian on a 112 px gray center crop
SUGGEST_FLOOR = 0.30        # below this the "best person" is noise and is not shown
NEAR_MISS_FLOOR = 0.40
OWN_FLOOR = 0.30            # a new reference must resemble the person's own photos at least this much
MIN_MARGIN = 0.05           # ...and beat the most similar OTHER person by this much
NO_REF_OTHER_LIMIT = 0.45   # person without any photo: nobody else may look this similar
MAIN_PHOTO_FLOOR = 0.30     # ...and resemble the main EduSchool photo, when there is one (stops reference drift:
                            # 2026-11-12, a frame 0.50-similar to one camera frame but 0.27 to the main photo
                            # caused 7 false attendance marks for another girl)


@dataclass
class Suggestion:
    person_id: str
    name: str
    score: float


@dataclass
class QueueItem:
    event_id: str
    camera_id: str
    created_at: datetime
    photo_path: str
    visitor_id: int | None
    best: Suggestion | None
    second: Suggestion | None
    crop_size: tuple[int, int]
    sharpness: float
    group_event_ids: list[str] = field(default_factory=list)
    group_photos: list[str] = field(default_factory=list)
    best_camera_refs: int = 0


@dataclass
class CheckResult:
    ok: bool
    own: float | None
    other: float | None
    other_person_id: str | None
    other_name: str | None
    reason: str


@lru_cache(maxsize=20000)
def _frame_quality_cached(path: str, mtime: float):
    return _frame_quality(Path(path))


def frame_quality(path: Path):
    try:
        return _frame_quality_cached(str(path), path.stat().st_mtime)
    except OSError:
        return None


def _frame_quality(path: Path) -> tuple[tuple[int, int], float] | None:
    image = cv2.imread(str(path))
    if image is None or image.size == 0:
        return None
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    side = min(height, width)
    top, left = (height - side) // 2, (width - side) // 2
    center = cv2.resize(gray[top:top + side, left:left + side], (112, 112), interpolation=cv2.INTER_AREA)
    return (width, height), float(cv2.Laplacian(center, cv2.CV_64F).var())


class FaceLabelingService:
    def __init__(self, engine, *, root: Path | None = None, reference_limit: int = 15,
                 recognition_threshold: float = 0.50, photo_service: EduSchoolPhotoService | None = None):
        self.engine = engine
        self.root = Path(root) if root is not None else PROJECT_ROOT
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)
        self.threshold = float(recognition_threshold)
        self.photos = photo_service or EduSchoolPhotoService(engine, reference_limit=reference_limit, root=self.root)

    # ---- gallery -------------------------------------------------------------------------------
    def gallery(self):
        """Active reference vectors with their catalog person ids (the ids add_local_photo needs)."""
        with self.Session() as session:
            rows = session.query(EduSchoolReferencePhoto.person_id, EduSchoolReferencePhoto.embedding,
                                 EduSchoolCatalogPerson.full_name).join(
                EduSchoolCatalogPerson, EduSchoolReferencePhoto.person_id == EduSchoolCatalogPerson.id
            ).filter(EduSchoolReferencePhoto.active.is_(True), EduSchoolCatalogPerson.active.is_(True)).all()
        owners, names, vectors = [], {}, []
        for person_id, embedding, name in rows:
            try:
                vectors.append(normalized_embedding(embedding))
            except (ValueError, TypeError, OverflowError):
                continue
            owners.append(person_id)
            names[person_id] = name
        matrix = np.vstack(vectors) if vectors else np.zeros((0, 512), dtype=np.float32)
        return np.asarray(owners), names, matrix

    @staticmethod
    def _grouping(owners):
        """Column order and group starts so a per-person maximum is one vectorised reduceat."""
        order = np.argsort(owners, kind="stable")
        sorted_owners = owners[order]
        starts = np.flatnonzero(np.r_[True, sorted_owners[1:] != sorted_owners[:-1]]) if len(owners) else np.array([], int)
        return order, starts, sorted_owners[starts] if len(owners) else np.array([])

    @classmethod
    def top_two(cls, vectors, owners, matrix):
        """For each row of `vectors`: (best person, score, second person, score); persons differ."""
        if not len(owners) or not len(vectors):
            return [(None, None, None, None)] * len(vectors)
        order, starts, people = cls._grouping(owners)
        sims = (np.asarray(vectors, dtype=np.float32) @ matrix.T)[:, order]
        per_person = np.maximum.reduceat(sims, starts, axis=1)
        result = []
        for row in per_person:
            if len(row) == 1:
                result.append((people[0], float(row[0]), None, None))
                continue
            top = np.argpartition(-row, 1)[:2]
            top = top[np.argsort(-row[top])]
            result.append((people[top[0]], float(row[top[0]]), people[top[1]], float(row[top[1]])))
        return result

    @classmethod
    def person_scores(cls, vector, owners, matrix) -> dict[str, float]:
        if not len(owners):
            return {}
        order, starts, people = cls._grouping(owners)
        per_person = np.maximum.reduceat((matrix @ vector)[order], starts)
        return {str(pid): float(score) for pid, score in zip(people, per_person)}

    def camera_ref_counts(self) -> dict[str, int]:
        """Active camera-frame (local) references per person; the per-person limit applies to these."""
        from sqlalchemy import func
        with self.Session() as session:
            rows = session.query(EduSchoolReferencePhoto.person_id, func.count()).filter(
                EduSchoolReferencePhoto.source == "local", EduSchoolReferencePhoto.active.is_(True)
            ).group_by(EduSchoolReferencePhoto.person_id).all()
        return {person_id: int(count) for person_id, count in rows}

    @property
    def reference_limit(self) -> int:
        return int(getattr(self.photos, "reference_limit", 15))

    def people(self) -> list[tuple[str, str]]:
        with self.Session() as session:
            rows = session.query(EduSchoolCatalogPerson.id, EduSchoolCatalogPerson.full_name).filter(
                EduSchoolCatalogPerson.active.is_(True)).order_by(EduSchoolCatalogPerson.full_name).all()
        return [(row[0], row[1]) for row in rows]

    def main_photo(self, person_id: str) -> str | None:
        """The person's EduSchool (catalog) photo: the reference everyone recognises them by."""
        with self.Session() as session:
            rows = session.query(EduSchoolReferencePhoto.photo_path).filter(
                EduSchoolReferencePhoto.person_id == person_id, EduSchoolReferencePhoto.source == "eduschool",
                EduSchoolReferencePhoto.active.is_(True)).order_by(EduSchoolReferencePhoto.created_at.desc()).all()
        return next((row[0] for row in rows if row[0] and (self.root / row[0]).is_file()), None)

    def camera_photos(self, person_id: str, limit: int = 3) -> list[str]:
        """Already confirmed camera frames (local references), newest first."""
        with self.Session() as session:
            rows = session.query(EduSchoolReferencePhoto.photo_path).filter(
                EduSchoolReferencePhoto.person_id == person_id, EduSchoolReferencePhoto.source == "local",
                EduSchoolReferencePhoto.active.is_(True)).order_by(EduSchoolReferencePhoto.created_at.desc()).all()
        return [row[0] for row in rows if row[0] and (self.root / row[0]).is_file()][:limit]

    def person_photos(self, person_id: str, limit: int = 3) -> list[str]:
        with self.Session() as session:
            rows = session.query(EduSchoolReferencePhoto.photo_path, EduSchoolReferencePhoto.source).filter(
                EduSchoolReferencePhoto.person_id == person_id, EduSchoolReferencePhoto.active.is_(True)
            ).order_by(EduSchoolReferencePhoto.source.desc(), EduSchoolReferencePhoto.created_at.desc()).all()
        return [path for path, _ in rows if path and (self.root / path).is_file()][:limit]

    # ---- queue ---------------------------------------------------------------------------------
    def queue(self, *, days: int = 3, camera_id: str | None = None, near_miss_only: bool = True,
              limit: int = 20) -> list[QueueItem]:
        since = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
        owners, names, matrix = self.gallery()
        with self.Session() as session:
            decided = {row[0] for row in session.query(FaceLabelDecision.event_id).filter(
                FaceLabelDecision.created_at >= since - timedelta(days=1)).all()}
            query = session.query(RecognitionEvent, UnknownFaceObservation).join(
                UnknownFaceObservation, UnknownFaceObservation.event_id == RecognitionEvent.id
            ).filter(RecognitionEvent.person_id.is_(None), RecognitionEvent.created_at >= since,
                     RecognitionEvent.photo_path.is_not(None), UnknownFaceObservation.embedding.is_not(None))
            if camera_id:
                query = query.filter(RecognitionEvent.camera_id == camera_id)
            rows = query.order_by(RecognitionEvent.created_at.desc()).limit(3000).all()
        groups: dict[object, QueueItem] = {}
        camera_refs = self.camera_ref_counts()
        pending, vectors = [], []
        for event, observation in rows:
            if event.id in decided:
                continue
            try:
                vectors.append(normalized_embedding(observation.embedding))
            except (ValueError, TypeError, OverflowError):
                continue
            pending.append((event, observation))
        scored = sorted(zip(pending, self.top_two(vectors, owners, matrix)), key=lambda item: -(item[1][1] or 0))
        for (event, observation), (pid1, s1, pid2, s2) in scored:
            if len(groups) >= limit * 2:
                break  # strongest suggestions first; enough cards for this page set
            best = Suggestion(str(pid1), names[pid1], s1) if pid1 is not None and s1 >= SUGGEST_FLOOR else None
            second = Suggestion(str(pid2), names[pid2], s2) if pid2 is not None else None
            if near_miss_only and (best is None or not NEAR_MISS_FLOOR <= best.score <= self.threshold):
                continue
            if best is not None and camera_refs.get(best.person_id, 0) >= self.reference_limit:
                continue  # already at the per-person limit: a "Yes" could not add anything
            # Reading the image is the expensive part, so it comes after the cheap score filter.
            path = self.root / event.photo_path
            quality = frame_quality(path) if path.is_file() else None
            if quality is None:
                continue
            (width, height), sharpness = quality
            if min(width, height) < MIN_CROP_SIDE or sharpness < MIN_SHARPNESS:
                continue
            # One card per unknown-visitor cluster (or per event when not clustered yet).
            key = ("visitor", observation.visitor_id) if observation.visitor_id else ("event", event.id)
            item = QueueItem(event.id, event.camera_id, event.created_at, event.photo_path, observation.visitor_id,
                             best, second, (width, height), sharpness, [event.id], [event.photo_path],
                             camera_refs.get(best.person_id, 0) if best else 0)
            current = groups.get(key)
            if current is None:
                groups[key] = item
                continue
            current.group_event_ids.append(event.id)
            if len(current.group_photos) < 4:
                current.group_photos.append(event.photo_path)
            if (item.best.score if item.best else 0) > (current.best.score if current.best else 0):
                item.group_event_ids, item.group_photos = current.group_event_ids, current.group_photos
                groups[key] = item
        items = sorted(groups.values(), key=lambda q: -(q.best.score if q.best else 0))
        return items[:limit]

    # ---- decisions -----------------------------------------------------------------------------
    def _event_vector(self, event_id: str):
        with self.Session() as session:
            row = session.query(RecognitionEvent, UnknownFaceObservation).join(
                UnknownFaceObservation, UnknownFaceObservation.event_id == RecognitionEvent.id
            ).filter(RecognitionEvent.id == event_id).first()
        if row is None or not row[0].photo_path or row[1].embedding is None:
            raise ValueError("Кадр больше недоступен (удалён по сроку хранения).")
        return row[0], normalized_embedding(row[1].embedding)

    def check(self, event_id: str, person_id: str) -> CheckResult:
        _, vector = self._event_vector(event_id)
        return self.check_vector(vector, person_id)

    def check_vector(self, vector, person_id: str) -> CheckResult:
        """Would this embedding be a safe reference for `person_id`? Same rule for the page and bundle imports."""
        vector = normalized_embedding(vector)
        owners, names, matrix = self.gallery()
        scores = self.person_scores(vector, owners, matrix)
        own = scores.get(person_id)
        others = [(pid, score) for pid, score in scores.items() if pid != person_id]
        other_pid, other = max(others, key=lambda item: item[1]) if others else (None, None)
        other_name = names.get(other_pid) if other_pid else None
        if own is None:
            ok = other is None or other < NO_REF_OTHER_LIMIT
            reason = ("У человека ещё нет фото; кадр не похож на других — можно добавить." if ok else
                      f"У человека ещё нет фото, а кадр похож на {other_name} ({other:.2f}). Нужна проверка админа.")
            return CheckResult(ok, None, other, other_pid, other_name, reason)
        if own < OWN_FLOOR:
            return CheckResult(False, own, other, other_pid, other_name,
                               f"Кадр мало похож на остальные фото этого человека ({own:.2f}). Нужна проверка админа.")
        main = self.main_photo_score(vector, person_id)
        if main is not None and main < MAIN_PHOTO_FLOOR:
            return CheckResult(False, own, other, other_pid, other_name,
                               f"Кадр мало похож на основное фото из EduSchool ({main:.2f}), хотя похож на один из кадров "
                               f"с камеры ({own:.2f}). Так «дрейфуют» эталоны к чужому человеку. Нужна проверка админа.")
        if other is not None and own - other < MIN_MARGIN:
            return CheckResult(False, own, other, other_pid, other_name,
                               f"Кадр похож на {other_name} ({other:.2f}) почти так же, как на этого человека ({own:.2f}). "
                               "Нужна проверка админа.")
        return CheckResult(True, own, other, other_pid, other_name, "Проверка пройдена.")

    def main_photo_score(self, vector, person_id: str) -> float | None:
        """Best similarity to the person's active EduSchool (catalog) photos; None when there is none."""
        with self.Session() as session:
            rows = session.query(EduSchoolReferencePhoto.embedding).filter(
                EduSchoolReferencePhoto.person_id == person_id, EduSchoolReferencePhoto.source == "eduschool",
                EduSchoolReferencePhoto.active.is_(True)).all()
        scores = []
        for (embedding,) in rows:
            try:
                scores.append(float(normalized_embedding(embedding) @ normalized_embedding(vector)))
            except (ValueError, TypeError, OverflowError):
                continue
        return max(scores) if scores else None

    def _log(self, session, *, event_id, decision, actor, person_id=None, reference_photo_id=None, detail=None,
             check: CheckResult | None = None) -> FaceLabelDecision:
        record = FaceLabelDecision(
            id=str(uuid.uuid4()), event_id=event_id, person_id=person_id, decision=decision,
            reference_photo_id=reference_photo_id, actor=(actor or "").strip()[:80] or "?", detail=detail,
            own_score=check.own if check else None, other_score=check.other if check else None,
            other_person_id=check.other_person_id if check else None,
        )
        session.add(record)
        return record

    def confirm(self, event_id: str, person_id: str, actor: str, *, group_event_ids=(), force: bool = False):
        """Add the frame as a reference if it passes the checks. Returns (decision, message)."""
        if not (actor or "").strip():
            raise ValueError("Укажите, кто размечает.")
        event, vector = self._event_vector(event_id)
        result = self.check(event_id, person_id)
        if force:
            with self.Session.begin() as session:
                for row in session.query(FaceLabelDecision).filter_by(event_id=event_id, decision="rejected").all():
                    row.decision = "closed"
        if not result.ok and not force:
            with self.Session.begin() as session:
                self._log(session, event_id=event_id, decision="rejected", actor=actor, person_id=person_id,
                          detail=result.reason, check=result)
            return "rejected", result.reason
        try:
            raw = (self.root / event.photo_path).read_bytes()
            record = self.photos.add_local_photo(person_id, raw, lambda _image: vector)
        except (OSError, ValueError) as error:
            with self.Session.begin() as session:
                self._log(session, event_id=event_id, decision="rejected", actor=actor, person_id=person_id,
                          detail=str(error), check=result)
            return "rejected", str(error)
        with self.Session.begin() as session:
            self._log(session, event_id=event_id, decision="forced" if force and not result.ok else "added",
                      actor=actor, person_id=person_id, reference_photo_id=record.id, detail=result.reason, check=result)
            # The other frames of the same pass are settled too, so they do not come back.
            for other_id in group_event_ids:
                if other_id != event_id:
                    self._log(session, event_id=other_id, decision="skip", actor=actor, person_id=person_id,
                              detail=f"Тот же проход, эталон добавлен из {event_id}")
        return "added", "Эталон добавлен и уже используется камерами."

    def dismiss(self, event_ids, actor: str, kind: str = "skip") -> None:
        if kind not in {"skip", "bad"}:
            raise ValueError("Unknown dismiss kind")
        if not (actor or "").strip():
            raise ValueError("Укажите, кто размечает.")
        with self.Session.begin() as session:
            for event_id in event_ids:
                self._log(session, event_id=event_id, decision=kind, actor=actor)

    def close_rejected(self, decision_id: str, actor: str) -> None:
        """Admin agrees with the automatic rejection: the frame stays out of the gallery."""
        with self.Session.begin() as session:
            decision = session.get(FaceLabelDecision, decision_id)
            if decision is None or decision.decision != "rejected":
                raise ValueError("Решение уже закрыто.")
            decision.decision = "closed"
            self._log(session, event_id=decision.event_id, decision="bad", actor=actor, person_id=decision.person_id,
                      detail=f"Админ подтвердил отказ {decision_id}")

    def cameras(self, days: int = 14) -> list[str]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        with self.Session() as session:
            rows = session.query(RecognitionEvent.camera_id).filter(RecognitionEvent.created_at >= since).distinct().all()
        return sorted(row[0] for row in rows)

    def undo(self, decision_id: str, actor: str) -> str:
        with self.Session.begin() as session:
            decision = session.get(FaceLabelDecision, decision_id)
            if decision is None or decision.decision not in {"added", "forced"} or not decision.reference_photo_id:
                raise ValueError("Отменить можно только добавленный эталон.")
            photo = session.get(EduSchoolReferencePhoto, decision.reference_photo_id)
            if photo is not None:
                photo.active = False
            decision.decision = "undone"
            self._log(session, event_id=decision.event_id, decision="undone", actor=actor, person_id=decision.person_id,
                      reference_photo_id=decision.reference_photo_id, detail=f"Отмена решения {decision_id}")
        return "Эталон выключен."

    def journal(self, *, limit: int = 100, decisions=None):
        with self.Session() as session:
            query = session.query(FaceLabelDecision, EduSchoolCatalogPerson.full_name, RecognitionEvent.photo_path).outerjoin(
                EduSchoolCatalogPerson, FaceLabelDecision.person_id == EduSchoolCatalogPerson.id
            ).join(RecognitionEvent, FaceLabelDecision.event_id == RecognitionEvent.id)
            if decisions:
                query = query.filter(FaceLabelDecision.decision.in_(list(decisions)))
            return query.order_by(FaceLabelDecision.created_at.desc()).limit(limit).all()
