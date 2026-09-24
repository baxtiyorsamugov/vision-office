"""Remove one superseded EduSchool directory snapshot after a branch switch."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from datetime import timezone
from pathlib import Path

from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.eduschool.photos import recognition_id
from database.manager import get_engine
from database.models import (
    EduSchoolCatalogPerson,
    EduSchoolCatalogSyncState,
    EduSchoolReferencePhoto,
    EduSchoolTurnstileOutbox,
    RecognitionEvent,
)


def purge_absent_catalog(engine, root: Path, expected_people: int, expected_photos: int, apply: bool = False) -> dict:
    if expected_people <= 0 or expected_photos < 0:
        raise ValueError("Expected counts must be positive (photos may be zero)")
    photo_root = (root / "data" / "persons" / "eduschool").resolve()
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        state = session.get(EduSchoolCatalogSyncState, 1)
        if state is None or state.last_success_at is None:
            raise ValueError("A successful current-branch sync is required")
        people = session.query(EduSchoolCatalogPerson).all()
        candidates = [person for person in people if person.source_status == "absent"]
        kept = [person for person in people if person.source_status != "absent"]
        if len(candidates) != expected_people or len(kept) != state.student_count + state.employee_count:
            raise ValueError("Catalog counts changed; inspect the branch snapshot before deleting")
        cutoff = state.last_success_at.replace(tzinfo=state.last_success_at.tzinfo or timezone.utc)
        if any(
            person.active or person.last_seen_at.replace(tzinfo=person.last_seen_at.tzinfo or timezone.utc) >= cutoff
            for person in candidates
        ):
            raise ValueError("Candidate is active or belongs to the latest snapshot")
        candidate_ids = [person.id for person in candidates]
        photos = session.query(EduSchoolReferencePhoto).filter(EduSchoolReferencePhoto.person_id.in_(candidate_ids)).all()
        if len(photos) != expected_photos:
            raise ValueError("Photo count changed; inspect the branch snapshot before deleting")
        recognition_ids = [recognition_id(person) for person in candidates]
        if session.query(RecognitionEvent.id).filter(RecognitionEvent.person_id.in_(recognition_ids)).first():
            raise ValueError("Old-branch recognition events exist; preserve the audit before deleting")
        if session.query(EduSchoolTurnstileOutbox.event_id).filter(
            EduSchoolTurnstileOutbox.person_id.in_(candidate_ids)
        ).first():
            raise ValueError("Old-branch attendance outbox rows exist; reconcile before deleting")

        def folder_for(person_id: str) -> Path:
            return photo_root / hashlib.sha256(person_id.encode()).hexdigest()[:20]

        folders = {folder_for(person_id) for person_id in candidate_ids}
        if folders.intersection(folder_for(person.id) for person in kept):
            raise ValueError("Photo folder overlaps with the current branch")
        for folder in folders:
            if folder.is_symlink() or not folder.resolve().is_relative_to(photo_root):
                raise ValueError("Unsafe photo folder")
        for photo in photos:
            path = Path(photo.photo_path)
            resolved = (path if path.is_absolute() else root / path).resolve()
            if resolved.parent != folder_for(photo.person_id) or not resolved.is_relative_to(photo_root):
                raise ValueError("Old photo path is outside its own catalog folder")

        result = {"people": len(candidates), "photos": len(photos), "folders": sum(folder.is_dir() for folder in folders)}
        if apply:
            deleted_photos = session.query(EduSchoolReferencePhoto).filter(
                EduSchoolReferencePhoto.person_id.in_(candidate_ids)
            ).delete(synchronize_session=False)
            deleted_people = session.query(EduSchoolCatalogPerson).filter(
                EduSchoolCatalogPerson.id.in_(candidate_ids)
            ).delete(synchronize_session=False)
            if deleted_photos != expected_photos or deleted_people != expected_people:
                raise ValueError("Deletion count changed; transaction rolled back")

    if apply:
        for folder in folders:
            if folder.is_dir():
                shutil.rmtree(folder)
        if any(folder.exists() for folder in folders):
            raise OSError("Some old photo folders remain after database deletion")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-people", type=int, required=True)
    parser.add_argument("--expected-photos", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="Delete after a database and photo backup")
    args = parser.parse_args()
    engine = get_engine()
    try:
        if args.apply and engine.dialect.name != "postgresql":
            raise ValueError("Live deletion requires PostgreSQL")
        result = purge_absent_catalog(
            engine, PROJECT_ROOT, args.expected_people, args.expected_photos, apply=args.apply
        )
        mode = "deleted" if args.apply else "dry_run"
        print(f"{mode}: people={result['people']} photos={result['photos']} folders={result['folders']}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
