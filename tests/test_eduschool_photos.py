import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.ai.engine import _choose_best_match, _load_known_faces
from core.ai.recognizer import FaceRecognizer
from core.edge.config import EdgeSettings
from core.edge.service import EdgeService
from core.eduschool.catalog import EduSchoolCatalogSettings, EduSchoolCatalogSync
from core.eduschool.photos import EduSchoolPhotoService, load_recognition_embeddings, recognition_id
from core.events import RecognitionEventStore
from database.models import AccessLogOutbox, EduSchoolCatalogPerson, EduSchoolReferencePhoto, RecognitionEvent, RemotePerson


BRANCH = "661d00dea4401645477617d9"
EXTERNAL_ID = "661d00dea440164547761701"
PERSON_ID = f"employee:{EXTERNAL_ID}"


class EduSchoolPhotoTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.engine = create_engine(f"sqlite:///{self.root / 'office.db'}")
        self.service = EduSchoolPhotoService(self.engine, reference_limit=2, root=self.root)
        with self.service.Session.begin() as session:
            session.add(EduSchoolCatalogPerson(
                id=PERSON_ID, person_type="employee", external_id=EXTERNAL_ID,
                full_name="Example Employee", source_status="active", active=True,
            ))
        image = np.full((160, 160, 3), 127, dtype=np.uint8)
        self.photo = cv2.imencode(".jpg", image)[1].tobytes()
        self.vector = np.ones(512, dtype=np.float32)

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_upload_is_local_and_available_to_matching_cache(self):
        record = self.service.add_local_photo(PERSON_ID, self.photo, lambda image: self.vector)
        self.assertTrue((self.root / record.photo_path).is_file())
        self.assertAlmostEqual(np.linalg.norm(record.embedding), 1.0, places=5)
        with self.service.Session() as session:
            identities, vectors = load_recognition_embeddings(session)
        self.assertEqual(identities, [{
            "name": "Example Employee", "person_id": f"edu:e:{EXTERNAL_ID}",
            "person_type": "eduschool_employee",
        }])
        self.assertEqual(len(vectors), 1)
        names, matrix = _load_known_faces(sessionmaker(bind=self.engine))
        self.assertEqual(names, identities)
        self.assertEqual(matrix.shape, (1, 512))

    def test_duplicate_limit_invalid_and_inactive_are_rejected(self):
        with self.assertRaises(ValueError):
            self.service.add_local_photo(PERSON_ID, b"bad", lambda image: self.vector)
        self.service.add_local_photo(PERSON_ID, self.photo, lambda image: self.vector)
        with self.assertRaisesRegex(ValueError, "уже добавлено"):
            self.service.add_local_photo(PERSON_ID, self.photo, lambda image: self.vector)
        second = cv2.imencode(".png", np.full((160, 160, 3), 128, dtype=np.uint8))[1].tobytes()
        self.service.add_local_photo(PERSON_ID, second, lambda image: self.vector)
        third = cv2.imencode(".png", np.full((160, 160, 3), 129, dtype=np.uint8))[1].tobytes()
        with self.assertRaisesRegex(ValueError, "лимит"):
            self.service.add_local_photo(PERSON_ID, third, lambda image: self.vector)
        with self.service.Session.begin() as session:
            session.get(EduSchoolCatalogPerson, PERSON_ID).active = False
        with self.assertRaisesRegex(ValueError, "активного"):
            self.service.add_local_photo(PERSON_ID, third, lambda image: self.vector)
        with self.service.Session() as session:
            identities, _ = load_recognition_embeddings(session)
        self.assertEqual(identities, [])

    def test_directory_sync_preserves_photo_and_deactivation_stops_recognition(self):
        record = self.service.add_local_photo(PERSON_ID, self.photo, lambda image: self.vector)
        sync = EduSchoolCatalogSync(EduSchoolCatalogSettings(
            enabled=True, branch_id=BRANCH, bearer_token="test", api_key="test"
        ), self.engine)
        row = {
            "_id": EXTERNAL_ID, "branchEmployee": {"branchId": BRANCH, "isActive": True},
            "fullName": "Updated Employee", "state": "active",
        }
        sync._fetch_page = lambda kind, page: {"data": {"total": 1 if kind == "employee" else 0, "data": [row] if kind == "employee" else []}}
        sync.sync_once()
        with self.service.Session() as session:
            self.assertEqual(session.get(EduSchoolReferencePhoto, record.id).person_id, PERSON_ID)
            self.assertEqual(load_recognition_embeddings(session)[0][0]["name"], "Updated Employee")
        sync._fetch_page = lambda kind, page: {"data": {"total": 0, "data": []}}
        sync.sync_once()
        with self.service.Session() as session:
            self.assertEqual(session.query(EduSchoolReferencePhoto).count(), 1)
            self.assertEqual(session.get(EduSchoolCatalogPerson, PERSON_ID).source_status, "absent")
            self.assertEqual(load_recognition_embeddings(session)[0], [])

    def test_existing_legacy_face_is_not_enrolled_twice(self):
        with self.service.Session.begin() as session:
            session.add(RemotePerson(
                id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", person_type="employee",
                fio="Legacy Employee", active=True, embedding=self.vector.tolist(),
            ))
        with self.assertRaisesRegex(ValueError, "уже есть"):
            self.service.add_local_photo(PERSON_ID, self.photo, lambda image: self.vector)
        with self.service.Session() as session:
            self.assertEqual(session.query(EduSchoolReferencePhoto).count(), 0)

    def test_events_stay_local_and_existing_identity_wins_close_match(self):
        self.service.add_local_photo(PERSON_ID, self.photo, lambda image: self.vector)
        with self.service.Session() as session:
            identity = load_recognition_embeddings(session)[0][0]
            self.assertLessEqual(len(recognition_id(session.get(EduSchoolCatalogPerson, PERSON_ID))), 36)
        event = RecognitionEventStore(self.engine).record(
            camera_id="entry", event_type="entry", identity=identity,
            confidence=0.8, embedding=self.vector, image=None,
        )
        self.assertIsNotNone(event)
        edge = EdgeService(EdgeSettings(
            enabled=True, base_url="https://example.test", device_api_key="test", device_id="device-test"
        ), self.engine)
        self.assertFalse(edge.queue_access_event(event))
        with edge.Session() as session:
            self.assertEqual(session.query(AccessLogOutbox).count(), 0)
            self.assertEqual(session.query(RecognitionEvent).count(), 1)
        identities = [
            {"person_id": "legacy", "name": "Existing"},
            {"person_id": identity["person_id"], "name": identity["name"]},
        ]
        self.assertEqual(_choose_best_match(identities, np.array([0.59, 0.62]), 0.4)[0], 0)
        self.assertEqual(_choose_best_match(identities, np.array([0.41, 0.53]), 0.4)[0], 0)
        self.assertEqual(_choose_best_match(identities, np.array([0.45, 0.80]), 0.4)[0], 1)

    def test_enrollment_requires_exactly_one_face(self):
        recognizer = FaceRecognizer.__new__(FaceRecognizer)
        recognizer.using_cuda = False

        class FakeApp:
            faces = []

            def get(self, image):
                return self.faces

        class FakeFace:
            embedding = np.ones(512, dtype=np.float32)

        recognizer.app = FakeApp()
        image = np.zeros((32, 32, 3), dtype=np.uint8)
        self.assertIsNone(recognizer.get_embedding(image, require_single=True))
        recognizer.app.faces = [FakeFace(), FakeFace()]
        self.assertIsNone(recognizer.get_embedding(image, require_single=True))
        recognizer.app.faces = [FakeFace()]
        self.assertIsNotNone(recognizer.get_embedding(image, require_single=True))


if __name__ == "__main__":
    unittest.main()
