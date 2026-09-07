import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from sqlalchemy import create_engine

from core.edge.config import EdgeSettings
from core.edge.service import EdgeRequestError, EdgeService
from database.models import AccessLogOutbox, RecognitionEvent, RemotePerson, RemotePersonReferencePhoto


class EdgeServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "edge.db"
        engine = create_engine(f"sqlite:///{db_path}")
        settings = EdgeSettings(
            enabled=True,
            base_url="https://example.test",
            device_api_key="test-key",
            device_id="device-123",
            event_cooldown_seconds=60,
        )
        self.service = EdgeService(settings, engine=engine)

    def tearDown(self):
        self.service.engine.dispose()
        self.tempdir.cleanup()

    def test_embedding_requires_512_finite_values(self):
        self.assertTrue(self.service._valid_embedding([0.1] * 512))
        self.assertFalse(self.service._valid_embedding([0.1] * 511))
        self.assertFalse(self.service._valid_embedding([float("nan")] * 512))

    def test_known_event_is_persisted_once_with_server_identifier(self):
        identity = {"person_id": "a0d0d2f4-6a2e-4f3f-a42a-990063865000", "person_type": "employee", "name": "Test"}
        embedding = np.zeros(512, dtype=np.float32)
        self.assertTrue(self.service.queue_recognition(identity, 0.94, embedding))
        self.assertFalse(self.service.queue_recognition(identity, 0.94, embedding))
        session = self.service.Session()
        try:
            event = session.query(AccessLogOutbox).one()
            body = json.loads(event.payload)
            self.assertEqual(body["person_id"], identity["person_id"])
            self.assertEqual(body["person_type"], "employee")
            self.assertNotIn("embedding", body)
            self.assertTrue(event.idempotency_key.startswith("device-123:"))
        finally:
            session.close()

    def test_unknown_event_is_never_queued_for_erp(self):
        embedding = np.full(512, 0.1, dtype=np.float32)
        self.assertFalse(self.service.queue_recognition(None, 0.2, embedding, subject_hint="unknown:42"))
        session = self.service.Session()
        try:
            self.assertEqual(session.query(AccessLogOutbox).count(), 0)
        finally:
            session.close()

    def test_known_event_payload_has_camera_and_idempotency_context(self):
        event = RecognitionEvent(
            id="e0d0d2f4-6a2e-4f3f-a42a-990063865000",
            camera_id="entry-camera",
            event_type="entry",
            person_id="a0d0d2f4-6a2e-4f3f-a42a-990063865000",
            person_type="employee",
            person_name="Test",
            subject_signature="a0d0d2f4-6a2e-4f3f-a42a-990063865000",
            confidence=0.94,
        )
        self.assertTrue(self.service.queue_access_event(event))
        session = self.service.Session()
        try:
            body = json.loads(session.query(AccessLogOutbox).one().payload)
            self.assertEqual(body["camera_id"], "entry-camera")
            self.assertEqual(body["event_type"], "entry")
            self.assertEqual(body["event_id"], event.id)
            self.assertEqual(body["edge_device_id"], "device-123")
        finally:
            session.close()

    def test_entry_and_exit_are_not_deduplicated_together(self):
        base = {
            "person_id": "a0d0d2f4-6a2e-4f3f-a42a-990063865000",
            "person_type": "employee",
            "person_name": "Test",
            "subject_signature": "ignored",
            "confidence": 0.9,
        }
        entry = RecognitionEvent(id="10000000-0000-0000-0000-000000000001", camera_id="entry", event_type="entry", **base)
        exit_event = RecognitionEvent(id="10000000-0000-0000-0000-000000000002", camera_id="exit", event_type="exit", **base)
        self.assertTrue(self.service.queue_access_event(entry))
        self.assertTrue(self.service.queue_access_event(exit_event))
        session = self.service.Session()
        try:
            self.assertEqual(session.query(AccessLogOutbox).count(), 2)
        finally:
            session.close()

    def test_sync_normalizes_remote_embedding(self):
        self.service._request_json = lambda *args, **kwargs: [{
            "id": "b0d0d2f4-6a2e-4f3f-a42a-990063865000",
            "person_type": "employee",
            "fio": "Remote Person",
            "active": True,
            "embedding": [0.1] * 512,
        }]
        self.assertTrue(self.service.sync_if_due())
        session = self.service.Session()
        try:
            person = session.get(RemotePerson, "b0d0d2f4-6a2e-4f3f-a42a-990063865000")
            self.assertEqual(person.embedding_status, "ready")
            self.assertAlmostEqual(float(np.linalg.norm(np.asarray(person.embedding))), 1.0, places=5)
        finally:
            session.close()

    def test_invalid_remote_person_without_photo_is_recorded_locally(self):
        session = self.service.Session()
        try:
            self.service._upsert_person(session, {
                "id": "c0d0d2f4-6a2e-4f3f-a42a-990063865000",
                "person_type": "employee",
                "fio": "Invalid Person",
                "embedding": [0.2] * 10,
            })
            session.commit()
            person = session.get(RemotePerson, "c0d0d2f4-6a2e-4f3f-a42a-990063865000")
            self.assertEqual(person.embedding_status, "invalid")
            self.assertIn("No valid embedding", person.embedding_error)
        finally:
            session.close()

    def test_photo_fallback_builds_and_caches_embedding(self):
        class FakeRecognizer:
            @staticmethod
            def get_embedding(_image):
                return np.ones(512, dtype=np.float32)

        self.service._recognizer = FakeRecognizer()
        self.service._download_reference_photo = lambda person_id, url: (
            np.zeros((112, 112, 3), dtype=np.uint8), f"data/persons/{person_id}.jpg"
        )
        session = self.service.Session()
        try:
            self.service._upsert_person(session, {
                "id": "f0d0d2f4-6a2e-4f3f-a42a-990063865000",
                "person_type": "employee",
                "fio": "Photo Person",
                "person_photo_url": "https://erp.example.test/photo.jpg",
            })
            session.commit()
            person = session.get(RemotePerson, "f0d0d2f4-6a2e-4f3f-a42a-990063865000")
            self.assertEqual(person.embedding_status, "ready")
            self.assertEqual(person.photo_path, "data/persons/f0d0d2f4-6a2e-4f3f-a42a-990063865000.jpg")
            self.assertAlmostEqual(float(np.linalg.norm(np.asarray(person.embedding))), 1.0, places=5)
        finally:
            session.close()

    def test_compact_sync_payload_preserves_cached_identity_and_embedding(self):
        person_id = "f1d0d2f4-6a2e-4f3f-a42a-990063865000"
        session = self.service.Session()
        try:
            self.service._upsert_person(session, {
                "id": person_id,
                "person_type": "employee",
                "fio": "Cached Person",
                "person_photo_url": "https://erp.example.test/person.jpg",
                "embedding": [0.1] * 512,
            })
            session.commit()
            self.service._upsert_person(session, {
                "id": person_id,
                "person_type": "employee",
                "active": True,
                "embedding": None,
            })
            session.commit()
            person = session.get(RemotePerson, person_id)
            self.assertEqual(person.fio, "Cached Person")
            self.assertEqual(person.photo_url, "https://erp.example.test/person.jpg")
            self.assertEqual(person.embedding_status, "ready")
            self.assertAlmostEqual(float(np.linalg.norm(np.asarray(person.embedding))), 1.0, places=5)
        finally:
            session.close()

    def test_local_reference_photo_adds_an_extra_matching_embedding(self):
        class FakeRecognizer:
            @staticmethod
            def get_embedding(_image):
                return np.ones(512, dtype=np.float32)

        person_id = "0f0d2f4-6a2e-4f3f-a42a-990063865000"
        session = self.service.Session()
        try:
            self.service._upsert_person(session, {
                "id": person_id,
                "person_type": "employee",
                "fio": "Enriched Person",
                "embedding": [0.1] * 512,
            })
            session.commit()
        finally:
            session.close()

        import cv2

        ok, encoded = cv2.imencode(".jpg", np.zeros((112, 112, 3), dtype=np.uint8))
        self.assertTrue(ok)
        self.service._recognizer = FakeRecognizer()
        self.service._local_reference_photo_path = lambda _person, _checksum, record_id: Path(self.tempdir.name) / f"{record_id}.jpg"
        with patch("core.edge.service.PROJECT_ROOT", Path(self.tempdir.name)):
            record = self.service.add_local_reference_photo(person_id, encoded.tobytes())

        session = self.service.Session()
        try:
            self.assertEqual(session.query(RemotePersonReferencePhoto).count(), 1)
            self.assertEqual(session.get(RemotePersonReferencePhoto, record.id).person_id, person_id)
        finally:
            session.close()
        identities, embeddings = self.service.cache_embeddings()
        self.assertEqual([item["person_id"] for item in identities], [person_id, person_id])
        self.assertEqual(embeddings.shape, (2, 512))

    def test_network_failure_is_retried_then_sent(self):
        identity = {"person_id": "d0d0d2f4-6a2e-4f3f-a42a-990063865000", "person_type": "employee", "name": "Test"}
        self.assertTrue(self.service.queue_recognition(identity, 0.94, np.zeros(512, dtype=np.float32)))
        self.service._request_json = lambda *args, **kwargs: (_ for _ in ()).throw(EdgeRequestError("offline"))
        self.assertEqual(self.service.deliver_due_events(), 0)
        session = self.service.Session()
        try:
            event = session.query(AccessLogOutbox).one()
            self.assertEqual(event.status, "retry")
            event.next_attempt_at = self.service._utcnow()
            session.commit()
        finally:
            session.close()
        self.service._request_json = lambda *args, **kwargs: None
        self.assertEqual(self.service.deliver_due_events(), 1)
        session = self.service.Session()
        try:
            self.assertEqual(session.query(AccessLogOutbox).one().status, "sent")
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
