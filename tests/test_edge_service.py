import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine

from core.edge.config import EdgeSettings
from core.edge.service import EdgeService
from database.models import AccessLogOutbox


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

    def test_unknown_event_contains_embedding_without_person_id(self):
        embedding = np.full(512, 0.1, dtype=np.float32)
        self.assertTrue(self.service.queue_recognition(None, 0.2, embedding, subject_hint="unknown:42"))
        session = self.service.Session()
        try:
            body = json.loads(session.query(AccessLogOutbox).one().payload)
            self.assertEqual(body["person_type"], "unknown")
            self.assertEqual(len(body["embedding"]), 512)
            self.assertNotIn("person_id", body)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
