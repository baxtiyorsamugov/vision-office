import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import create_engine

from core.eduschool.photos import EduSchoolPhotoService
from core.face_labeling import FaceLabelingService
from database.models import (EduSchoolCatalogPerson, EduSchoolReferencePhoto, FaceLabelDecision, RecognitionEvent,
                             UnknownFaceObservation)


def axis(i):
    v = np.zeros(512, dtype=np.float32)
    v[i] = 1.0
    return v


def mix(*parts):
    v = sum(w * axis(i) for i, w in parts)
    return (v / np.linalg.norm(v)).astype(np.float32)


class FaceLabelingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.engine = create_engine(f"sqlite:///{self.root / 'office.db'}")
        photos = EduSchoolPhotoService(self.engine, reference_limit=15, root=self.root)
        self.service = FaceLabelingService(self.engine, root=self.root, recognition_threshold=0.50, photo_service=photos)
        rng = np.random.default_rng(1)
        self.sharp = cv2.imencode(".jpg", rng.integers(0, 255, (130, 100, 3), dtype=np.uint8))[1].tobytes()
        with self.service.Session.begin() as session:
            for pid, ext, name, vec in (("student:a", "a" * 24, "Alpha Student", axis(0)), ("student:b", "b" * 24, "Beta Student", axis(1))):
                session.add(EduSchoolCatalogPerson(id=pid, person_type="student", external_id=ext, full_name=name,
                                                   source_status="active", active=True))
                session.flush()
                session.add(EduSchoolReferencePhoto(id=f"ref-{pid}", person_id=pid, photo_path=f"data/persons/{pid}.jpg",
                                                    source="eduschool", image_checksum=pid, embedding=vec.tolist(), active=True))

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def add_event(self, event_id, vector, image=None):
        path = self.root / "data" / "events" / f"{event_id}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image if image is not None else self.sharp)
        with self.service.Session.begin() as session:
            session.add(RecognitionEvent(id=event_id, camera_id="cam", event_type="entry", person_type="unknown",
                                         subject_signature=f"unknown:{event_id}", photo_path=f"data/events/{event_id}.jpg"))
            session.flush()
            session.add(UnknownFaceObservation(id=f"obs-{event_id}", event_id=event_id, embedding=vector.tolist()))

    def test_queue_shows_only_usable_near_misses(self):
        self.add_event("near", mix((0, 0.46), (5, 0.888)))
        self.add_event("far", mix((0, 0.20), (6, 0.98)))
        self.add_event("known-level", mix((0, 0.70), (7, 0.71)))
        tiny = cv2.imencode(".jpg", np.random.default_rng(2).integers(0, 255, (40, 40, 3), dtype=np.uint8))[1].tobytes()
        self.add_event("tiny", mix((0, 0.46), (8, 0.888)), image=tiny)
        flat = cv2.imencode(".jpg", np.full((130, 100, 3), 120, dtype=np.uint8))[1].tobytes()
        self.add_event("blurry", mix((0, 0.46), (9, 0.888)), image=flat)
        items = self.service.queue(days=3)
        self.assertEqual([i.event_id for i in items], ["near"])
        self.assertEqual(items[0].best.person_id, "student:a")
        self.assertAlmostEqual(items[0].best.score, 0.46, places=2)
        self.assertEqual(len(self.service.queue(days=3, near_miss_only=False)), 3)  # near, far(noise hidden), known-level

    def test_confirm_adds_reference_and_hides_frame(self):
        self.add_event("near", mix((0, 0.46), (5, 0.888)))
        decision, message = self.service.confirm("near", "student:a", "Teacher")
        self.assertEqual(decision, "added", message)
        with self.service.Session() as session:
            refs = session.query(EduSchoolReferencePhoto).filter_by(person_id="student:a", source="local", active=True).all()
            self.assertEqual(len(refs), 1)
            log = session.query(FaceLabelDecision).filter_by(event_id="near").one()
            self.assertEqual((log.decision, log.actor), ("added", "Teacher"))
        self.assertEqual(self.service.queue(days=3), [])

    def test_frame_closer_to_someone_else_is_rejected_then_admin_can_force(self):
        # Looks like Alpha (0.42) more than Beta (0.40): labeling it Beta must not poison Beta's gallery.
        self.add_event("ambiguous", mix((0, 0.42), (1, 0.40), (5, 0.81)))
        decision, message = self.service.confirm("ambiguous", "student:b", "Teacher")
        self.assertEqual(decision, "rejected")
        self.assertIn("Alpha Student", message)
        with self.service.Session() as session:
            self.assertEqual(session.query(EduSchoolReferencePhoto).filter_by(source="local").count(), 0)
        decision, _ = self.service.confirm("ambiguous", "student:b", "Admin", force=True)
        self.assertEqual(decision, "added")
        with self.service.Session() as session:
            self.assertEqual(session.query(FaceLabelDecision).filter_by(decision="forced").count(), 1)

    def test_undo_switches_reference_off(self):
        self.add_event("near", mix((0, 0.46), (5, 0.888)))
        self.service.confirm("near", "student:a", "Teacher")
        added = self.service.journal(decisions=["added"])[0][0]
        self.service.undo(added.id, "Admin")
        with self.service.Session() as session:
            self.assertFalse(session.get(EduSchoolReferencePhoto, added.reference_photo_id).active)

    def test_dismiss_and_actor_required(self):
        self.add_event("near", mix((0, 0.46), (5, 0.888)))
        with self.assertRaises(ValueError):
            self.service.dismiss(["near"], "  ")
        self.service.dismiss(["near"], "Guard", kind="bad")
        self.assertEqual(self.service.queue(days=3), [])


if __name__ == "__main__":
    unittest.main()
