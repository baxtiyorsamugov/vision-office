import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import (
    Base,
    EduSchoolCatalogPerson,
    EduSchoolCatalogSyncState,
    EduSchoolReferencePhoto,
    RecognitionEvent,
)
from tools.purge_eduschool_absent import purge_absent_catalog


class PurgeEduSchoolAbsentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.engine = create_engine(f"sqlite:///{self.root / 'office.db'}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.old_id = "employee:" + "a" * 24
        self.new_id = "employee:" + "b" * 24
        self.old_folder = self._folder(self.old_id)
        self.new_folder = self._folder(self.new_id)
        old_path = self.old_folder / "old.jpg"
        new_path = self.new_folder / "new.jpg"
        self.old_folder.mkdir(parents=True)
        self.new_folder.mkdir(parents=True)
        old_path.write_bytes(b"old")
        new_path.write_bytes(b"new")
        with self.Session.begin() as session:
            session.add(EduSchoolCatalogSyncState(
                id=1, last_success_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
                student_count=0, employee_count=1,
            ))
            session.add_all([
                EduSchoolCatalogPerson(
                    id=self.old_id, person_type="employee", external_id="a" * 24,
                    full_name="Old Person", source_status="absent", active=False,
                    last_seen_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
                ),
                EduSchoolCatalogPerson(
                    id=self.new_id, person_type="employee", external_id="b" * 24,
                    full_name="New Person", source_status="active", active=True,
                    last_seen_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
                ),
            ])
            session.add_all([
                EduSchoolReferencePhoto(
                    id="old-photo", person_id=self.old_id, photo_path=str(old_path.relative_to(self.root)),
                    image_checksum="a" * 64, embedding=[1.0],
                ),
                EduSchoolReferencePhoto(
                    id="new-photo", person_id=self.new_id, photo_path=str(new_path.relative_to(self.root)),
                    image_checksum="b" * 64, embedding=[1.0],
                ),
            ])

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _folder(self, person_id):
        return self.root / "data" / "persons" / "eduschool" / hashlib.sha256(person_id.encode()).hexdigest()[:20]

    def test_dry_run_then_apply_removes_only_old_catalog_and_files(self):
        self.assertEqual(purge_absent_catalog(self.engine, self.root, 1, 1), {
            "people": 1, "photos": 1, "folders": 1,
        })
        self.assertTrue(self.old_folder.is_dir())
        self.assertEqual(purge_absent_catalog(self.engine, self.root, 1, 1, apply=True)["people"], 1)
        with self.Session() as session:
            self.assertIsNone(session.get(EduSchoolCatalogPerson, self.old_id))
            self.assertIsNone(session.get(EduSchoolReferencePhoto, "old-photo"))
            self.assertIsNotNone(session.get(EduSchoolCatalogPerson, self.new_id))
            self.assertIsNotNone(session.get(EduSchoolReferencePhoto, "new-photo"))
        self.assertFalse(self.old_folder.exists())
        self.assertTrue(self.new_folder.joinpath("new.jpg").is_file())

    def test_count_mismatch_does_not_delete(self):
        with self.assertRaisesRegex(ValueError, "Catalog counts changed"):
            purge_absent_catalog(self.engine, self.root, 2, 1, apply=True)
        self.assertTrue(self.old_folder.is_dir())

    def test_old_recognition_event_blocks_deletion(self):
        with self.Session.begin() as session:
            session.add(RecognitionEvent(
                id="event", camera_id="reception_01", event_type="entry",
                person_id="edu:e:" + "a" * 24, person_type="eduschool_employee",
                subject_signature="old-person",
            ))
        with self.assertRaisesRegex(ValueError, "recognition events exist"):
            purge_absent_catalog(self.engine, self.root, 1, 1, apply=True)
        self.assertTrue(self.old_folder.is_dir())

    def test_unsafe_photo_path_blocks_deletion(self):
        with self.Session.begin() as session:
            session.get(EduSchoolReferencePhoto, "old-photo").photo_path = "data/persons/eduschool/unrelated.jpg"
        with self.assertRaisesRegex(ValueError, "outside its own catalog folder"):
            purge_absent_catalog(self.engine, self.root, 1, 1, apply=True)
        self.assertTrue(self.old_folder.is_dir())


if __name__ == "__main__":
    unittest.main()
