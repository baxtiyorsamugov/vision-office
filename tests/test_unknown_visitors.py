import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from core.events import RecognitionEventStore
from core.unknown_visitors import UnknownVisitorService, UnknownVisitorSettings, utc_now
from database.migrations import run_migrations
from database.models import Employee, RecognitionEvent, UnknownFaceObservation, UnknownVisitor


class UnknownVisitorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.engine = create_engine(f"sqlite:///{self.root / 'unknown.db'}")
        self.settings = UnknownVisitorSettings(poll_interval_seconds=1, backfill_batch_size=1)
        self.service = UnknownVisitorService(engine=self.engine, settings=self.settings)
        self.store = RecognitionEventStore(self.engine, cooldown_seconds=0, track_unknown_observations=True)

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    @staticmethod
    def _vector(index: int) -> np.ndarray:
        value = np.zeros(512, dtype=np.float32)
        value[index] = 1.0
        return value

    def _record_unknown(self, hint: str, vector: np.ndarray):
        event = self.store.record(
            camera_id="entry", event_type="entry", identity=None,
            confidence=0.2, embedding=vector, image=None, subject_hint=hint,
        )
        self.assertIsNotNone(event)
        return event

    def test_migrations_create_unknown_visitor_tables(self):
        tables = set(inspect(self.engine).get_table_names())
        self.assertTrue({"unknown_visitors", "unknown_face_observations", "unknown_visitor_visits"}.issubset(tables))

    def test_similar_faces_cluster_and_different_faces_stay_separate(self):
        self._record_unknown("unknown:one", self._vector(0))
        self._record_unknown("unknown:two", self._vector(0))
        self._record_unknown("unknown:three", self._vector(1))
        self.assertEqual(self.service.process_pending(), 3)
        session = sessionmaker(bind=self.engine)()
        try:
            visitors = session.query(UnknownVisitor).filter(UnknownVisitor.state == "active").all()
            self.assertEqual(len(visitors), 2)
            self.assertEqual(sorted(item.observation_count for item in visitors), [1, 2])
        finally:
            session.close()

    def test_visit_window_counts_one_arrival_for_repeated_observations(self):
        first = self._record_unknown("unknown:one", self._vector(0))
        second = self._record_unknown("unknown:two", self._vector(0))
        third = self._record_unknown("unknown:three", self._vector(0))
        session = sessionmaker(bind=self.engine)()
        try:
            base = utc_now()
            for event_id, moment in ((first.id, base), (second.id, base + timedelta(minutes=5)), (third.id, base + timedelta(minutes=16))):
                event = session.get(RecognitionEvent, event_id)
                observation = session.query(UnknownFaceObservation).filter_by(event_id=event_id).one()
                event.created_at = moment
                observation.observed_at = moment
            session.commit()
        finally:
            session.close()
        self.service.process_pending()
        session = sessionmaker(bind=self.engine)()
        try:
            visitor = session.query(UnknownVisitor).filter_by(state="active").one()
            self.assertEqual(visitor.visit_count, 2)
        finally:
            session.close()

    def test_split_and_merge_rebuild_counts(self):
        self._record_unknown("unknown:one", self._vector(0))
        self._record_unknown("unknown:two", self._vector(0))
        self._record_unknown("unknown:three", self._vector(1))
        self.service.process_pending()
        session = sessionmaker(bind=self.engine)()
        try:
            visitors = session.query(UnknownVisitor).filter_by(state="active").order_by(UnknownVisitor.id).all()
            grouped = next(item for item in visitors if item.observation_count == 2)
            other = next(item for item in visitors if item.id != grouped.id)
            observation_id = session.query(UnknownFaceObservation.id).filter_by(visitor_id=grouped.id).first()[0]
        finally:
            session.close()
        split = self.service.split(grouped.id, [observation_id])
        session = sessionmaker(bind=self.engine)()
        try:
            self.assertEqual(session.get(UnknownVisitor, grouped.id).observation_count, 1)
            self.assertEqual(split.observation_count, 1)
        finally:
            session.close()
        merged = self.service.merge(split.id, other.id)
        self.assertEqual(merged.observation_count, 2)

    def test_convert_keeps_unknown_history_and_creates_local_employee(self):
        event = self._record_unknown("unknown:one", self._vector(0))
        image_path = self.root / "data" / "events" / "sample.jpg"
        image_path.parent.mkdir(parents=True)
        self.assertTrue(cv2.imwrite(str(image_path), np.zeros((128, 128, 3), dtype=np.uint8)))
        session = sessionmaker(bind=self.engine)()
        try:
            stored_event = session.get(RecognitionEvent, event.id)
            stored_event.photo_path = "data/events/sample.jpg"
            session.commit()
        finally:
            session.close()
        self.service.process_pending()
        session = sessionmaker(bind=self.engine)()
        try:
            visitor = session.query(UnknownVisitor).filter_by(state="active").one()
            observation_id = session.query(UnknownFaceObservation.id).filter_by(visitor_id=visitor.id).one()[0]
        finally:
            session.close()
        with patch("core.unknown_visitors.PROJECT_ROOT", self.root):
            employee = self.service.convert_to_local_employee(visitor.id, "Local Person", "Сотрудник", observation_id)
        session = sessionmaker(bind=self.engine)()
        try:
            self.assertEqual(session.get(UnknownVisitor, visitor.id).state, "converted")
            self.assertEqual(session.get(Employee, employee.id).full_name, "Local Person")
            self.assertEqual(session.get(RecognitionEvent, event.id).person_type, "unknown")
        finally:
            session.close()

    def test_backfill_marks_missing_photo_failed_and_valid_photo_pending(self):
        session = sessionmaker(bind=self.engine)()
        try:
            valid = RecognitionEvent(
                id="00000000-0000-0000-0000-000000000011", camera_id="entry", event_type="entry",
                person_type="unknown", subject_signature="unknown:valid", photo_path="data/events/valid.jpg", created_at=utc_now(),
            )
            missing = RecognitionEvent(
                id="00000000-0000-0000-0000-000000000012", camera_id="entry", event_type="entry",
                person_type="unknown", subject_signature="unknown:missing", photo_path="data/events/missing.jpg", created_at=utc_now(),
            )
            session.add_all([valid, missing])
            session.commit()
        finally:
            session.close()

        photo_path = self.root / "data" / "events" / "valid.jpg"
        photo_path.parent.mkdir(parents=True)
        self.assertTrue(cv2.imwrite(str(photo_path), np.zeros((128, 128, 3), dtype=np.uint8)))

        class FakeRecognizer:
            def get_embedding(self, _image):
                return UnknownVisitorTests._vector(0)

        with patch("core.unknown_visitors.PROJECT_ROOT", self.root):
            self.service._recognizer = FakeRecognizer()
            self.assertEqual(self.service.backfill_missing(limit=2), 2)
        session = sessionmaker(bind=self.engine)()
        try:
            statuses = sorted(item.processing_status for item in session.query(UnknownFaceObservation).all())
            self.assertEqual(statuses, ["failed", "pending"])
        finally:
            session.close()

    def test_retention_removes_biometric_sample_and_archives_empty_card(self):
        event = self._record_unknown("unknown:expired", self._vector(0))
        self.service.process_pending()
        session = sessionmaker(bind=self.engine)()
        try:
            observation = session.query(UnknownFaceObservation).filter_by(event_id=event.id).one()
            observation.observed_at = utc_now() - timedelta(days=31)
            session.get(RecognitionEvent, event.id).created_at = observation.observed_at
            visitor_id = observation.visitor_id
            session.commit()
        finally:
            session.close()
        self.assertEqual(self.service.purge_expired(), 1)
        session = sessionmaker(bind=self.engine)()
        try:
            observation = session.query(UnknownFaceObservation).filter_by(event_id=event.id).one()
            self.assertIsNone(observation.embedding)
            self.assertEqual(observation.processing_status, "expired")
            self.assertEqual(session.get(UnknownVisitor, visitor_id).state, "archived")
        finally:
            session.close()
if __name__ == "__main__":
    unittest.main()
