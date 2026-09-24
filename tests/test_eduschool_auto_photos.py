import tempfile
import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from sqlalchemy import create_engine

from core.ai.engine import _load_known_faces
from core.eduschool.auto_photos import EduSchoolAutoPhotoService, InvalidSourcePhoto, source_photo_url
from core.eduschool.catalog import EduSchoolCatalogSettings, EduSchoolCatalogSync
from database.models import EduSchoolCatalogPerson, EduSchoolReferencePhoto


BRANCH = "661d00dea4401645477617d9"
EXTERNAL_ID = "661d00dea440164547761701"
PERSON_ID = f"employee:{EXTERNAL_ID}"
SOURCE_PATH = f"org-{BRANCH}/uploads/file-abc123.jpg"


class EduSchoolAutoPhotoTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.engine = create_engine(f"sqlite:///{self.root / 'photo.db'}")
        self.settings = EduSchoolCatalogSettings(enabled=True, branch_id=BRANCH, bearer_token="test", api_key="test")
        self.service = EduSchoolAutoPhotoService(self.settings, self.engine, root=self.root)
        with self.service.Session.begin() as session:
            session.add(EduSchoolCatalogPerson(
                id=PERSON_ID, person_type="employee", external_id=EXTERNAL_ID,
                full_name="Example Employee", image_url=SOURCE_PATH,
                source_status="active", active=True, source_photo_status="pending",
            ))
        self.photo = cv2.imencode(".jpg", np.full((160, 160, 3), 127, dtype=np.uint8))[1].tobytes()
        self.vector = np.ones(512, dtype=np.float32)

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_source_url_is_confined_to_backend_uploads(self):
        expected = f"https://backend.eduschool.uz/uploads/{SOURCE_PATH}"
        self.assertEqual(source_photo_url(self.settings.base_url, SOURCE_PATH), expected)
        self.assertEqual(source_photo_url(self.settings.base_url, expected), expected)
        for path in ("https://other.test/face.jpg", "../private.jpg", "org-" + BRANCH + "/uploads/../secret.jpg", "//other.test/x.jpg"):
            with self.subTest(path=path), self.assertRaises(InvalidSourcePhoto):
                source_photo_url(self.settings.base_url, path)

    @patch("core.eduschool.auto_photos.download_source_photo")
    def test_conflicting_face_is_not_enrolled_for_second_person(self, download):
        download.return_value = self.photo
        self.assertEqual(self.service.process_one(PERSON_ID, lambda image: self.vector), "ready")
        second_id = "employee:661d00dea440164547761702"
        with self.service.Session.begin() as session:
            session.add(EduSchoolCatalogPerson(
                id=second_id, person_type="employee", external_id=second_id.split(":")[1],
                full_name="Another Employee", image_url=SOURCE_PATH,
                source_status="active", active=True, source_photo_status="pending",
            ))
        self.assertEqual(self.service.process_one(second_id, lambda image: self.vector), "invalid")
        with self.service.Session() as session:
            self.assertEqual(session.query(EduSchoolReferencePhoto).count(), 1)
            self.assertIn("conflicts", session.get(EduSchoolCatalogPerson, second_id).source_photo_error)

    @patch("core.eduschool.auto_photos.download_source_photo")
    def test_reactivation_rechecks_conflicts(self, download):
        download.return_value = self.photo
        self.service.process_one(PERSON_ID, lambda image: self.vector)
        second_id = "employee:661d00dea440164547761702"
        with self.service.Session.begin() as session:
            session.query(EduSchoolReferencePhoto).filter_by(person_id=PERSON_ID).one().active = False
            session.get(EduSchoolCatalogPerson, PERSON_ID).source_photo_status = "pending"
            session.add(EduSchoolCatalogPerson(
                id=second_id, person_type="employee", external_id=second_id.split(":")[1],
                full_name="Another Employee", source_status="active", active=True,
            ))
            session.add(EduSchoolReferencePhoto(
                id="second-photo", person_id=second_id, photo_path="unused.jpg",
                image_checksum="different", embedding=(self.vector / np.linalg.norm(self.vector)).tolist(),
                source="local", active=True,
            ))
        self.assertEqual(self.service.process_one(PERSON_ID, lambda image: self.vector), "invalid")
        with self.service.Session() as session:
            self.assertFalse(session.query(EduSchoolReferencePhoto).filter_by(person_id=PERSON_ID).one().active)

    @patch("core.eduschool.auto_photos.download_source_photo")
    def test_disabled_manual_photo_is_not_silently_reenabled(self, download):
        download.return_value = self.photo
        with self.service.Session.begin() as session:
            session.add(EduSchoolReferencePhoto(
                id="disabled-local", person_id=PERSON_ID, photo_path="unused.jpg",
                image_checksum=hashlib.sha256(self.photo).hexdigest(),
                embedding=(self.vector / np.linalg.norm(self.vector)).tolist(),
                source="local", active=False,
            ))
        self.assertEqual(self.service.process_one(PERSON_ID, lambda image: self.vector), "invalid")
        with self.service.Session() as session:
            self.assertFalse(session.get(EduSchoolReferencePhoto, "disabled-local").active)

    @patch("core.eduschool.auto_photos.download_source_photo")
    def test_source_image_enrolls_idempotently_and_matches(self, download):
        download.return_value = self.photo
        self.assertEqual(self.service.next_person_id(), PERSON_ID)
        self.assertEqual(self.service.process_one(PERSON_ID, lambda image: self.vector), "ready")
        self.assertIsNone(self.service.next_person_id())
        with self.service.Session() as session:
            person = session.get(EduSchoolCatalogPerson, PERSON_ID)
            record = session.query(EduSchoolReferencePhoto).one()
            self.assertEqual(person.source_photo_status, "ready")
            self.assertEqual(record.source, "eduschool")
            self.assertTrue((self.root / record.photo_path).is_file())
        names, matrix = _load_known_faces(self.service.Session)
        self.assertEqual(names[0]["person_id"], f"edu:e:{EXTERNAL_ID}")
        self.assertEqual(matrix.shape, (1, 512))
        self.assertEqual(self.service.process_one(PERSON_ID, lambda image: self.vector), "ready")
        with self.service.Session() as session:
            self.assertEqual(session.query(EduSchoolReferencePhoto).count(), 1)

    @patch("core.eduschool.auto_photos.download_source_photo")
    def test_no_face_or_network_failure_has_status_and_no_embedding(self, download):
        download.return_value = self.photo
        self.assertEqual(self.service.process_one(PERSON_ID, lambda image: None), "invalid")
        self.assertIsNone(self.service.next_person_id())
        with self.service.Session.begin() as session:
            session.get(EduSchoolCatalogPerson, PERSON_ID).source_photo_status = "pending"
        download.side_effect = ConnectionError("offline")
        self.assertEqual(self.service.process_one(PERSON_ID, lambda image: self.vector), "failed")
        with self.service.Session() as session:
            person = session.get(EduSchoolCatalogPerson, PERSON_ID)
            self.assertIsNotNone(person.source_photo_retry_at)
            self.assertEqual(session.query(EduSchoolReferencePhoto).count(), 0)

    @patch("core.eduschool.auto_photos.download_source_photo")
    def test_changed_source_deactivates_only_auto_photo(self, download):
        download.return_value = self.photo
        self.service.process_one(PERSON_ID, lambda image: self.vector)
        sync = EduSchoolCatalogSync(self.settings, self.engine)
        row = {
            "_id": EXTERNAL_ID, "branchEmployee": {"branchId": BRANCH, "isActive": True},
            "fullName": "Example Employee", "state": "active", "imageUrl": SOURCE_PATH.replace("abc123", "def456"),
        }
        sync._fetch_page = lambda kind, page: {"data": {"total": int(kind == "employee"), "data": [row] if kind == "employee" else []}}
        sync.sync_once()
        with self.service.Session() as session:
            person = session.get(EduSchoolCatalogPerson, PERSON_ID)
            self.assertEqual(person.source_photo_status, "pending")
            self.assertFalse(session.query(EduSchoolReferencePhoto).one().active)
        self.assertEqual(_load_known_faces(self.service.Session)[0], [])


if __name__ == "__main__":
    unittest.main()
