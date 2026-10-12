"""Bounded source-photo enrollment for an existing local EduSchool catalog."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def candidates(session, person_type):
    from sqlalchemy import or_
    from database.models import EduSchoolCatalogPerson as Person, EduSchoolReferencePhoto as Photo

    enrolled = session.query(Photo.id).filter(Photo.person_id == Person.id, Photo.active.is_(True)).exists()
    return session.query(Person).filter(
        Person.person_type == person_type,
        Person.active.is_(True),
        Person.image_url.is_not(None),
        Person.image_url != "",
        Person.source_photo_status.in_(("pending", "failed")),
        or_(Person.source_photo_retry_at.is_(None), Person.source_photo_retry_at <= datetime.now(timezone.utc)),
        ~enrolled,
    ).order_by(Person.id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--person-type", choices=("student", "employee"), default="student")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000:
        parser.error("limit must be between 1 and 10000")
    if args.apply and (not args.output or args.output.exists()):
        parser.error("--apply requires a new --output JSON file")
    from sqlalchemy.orm import sessionmaker
    from database.manager import get_engine
    from database.models import EduSchoolCatalogPerson

    engine = get_engine()
    Session = sessionmaker(bind=engine)
    with Session() as session:
        query = candidates(session, args.person_type)
        total = query.count()
        ids = [person.id for person in query.limit(args.limit).all()]
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "person_type": args.person_type,
                      "eligible": total, "selected": len(ids)}), flush=True)
    if not args.apply:
        engine.dispose()
        return
    os.environ["VISION_OFFICE_DEVICE"] = args.device
    from core.ai.recognizer import FaceRecognizer
    from core.eduschool.auto_photos import EduSchoolAutoPhotoService
    from core.eduschool.catalog import load_settings

    # Reuse the enrollment service, never the attendance/turnstile worker.
    service = EduSchoolAutoPhotoService(load_settings(), engine=engine, root=Path.cwd())
    logging.getLogger("vision_office.eduschool.photos").setLevel(logging.ERROR)
    recognizer = FaceRecognizer(use_cuda=args.device == "cuda") if ids else None
    counts, reasons, results = Counter(), Counter(), []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "person_type": args.person_type,
              "eligible": total, "selected": len(ids), "complete": False, "results": results}

    def checkpoint():
        report.update(counts=dict(counts), reasons=dict(reasons), processed=len(results))
        report["face_device"] = "cuda" if recognizer and recognizer.using_cuda else "cpu"
        temporary = args.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(args.output)

    checkpoint()
    try:
        for person_id in ids:
            # Recheck before downloading: another enrollment may have finished meanwhile.
            with Session() as session:
                still_eligible = candidates(session, args.person_type).filter(EduSchoolCatalogPerson.id == person_id).first()
            status = service.process_one(person_id, lambda image: recognizer.get_embedding(image, require_single=True)) \
                if still_eligible is not None else "skipped"
            with Session() as session:
                person = session.get(EduSchoolCatalogPerson, person_id)
                reason = person.source_photo_error if person and status in {"failed", "invalid"} else None
            counts[status] += 1
            if reason:
                reasons[reason] += 1
            results.append({"person_id": person_id, "status": status, "reason": reason})
            checkpoint()
            if len(results) % 25 == 0 or len(results) == len(ids):
                print(json.dumps({"processed": len(results), "selected": len(ids), "counts": dict(counts)}), flush=True)
        report["complete"] = True
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        checkpoint()
        print(json.dumps({"complete": True, "counts": dict(counts), "reasons": dict(reasons)}), flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
