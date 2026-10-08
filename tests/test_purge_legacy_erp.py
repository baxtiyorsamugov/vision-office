import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database.models import Base, RemotePerson, Employee, EduSchoolCatalogPerson, RecognitionEvent
from tools.purge_legacy_erp import purge_legacy


class LegacyPurgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.identifier = str(uuid.uuid4())
        self.photo = self.root / f'data/persons/{self.identifier}.jpg'
        self.photo.parent.mkdir(parents=True)
        self.photo.write_bytes(b'test photo')
        self.backup = self.root / 'data/backups/legacy.zip'
        with Session(self.engine) as session:
            session.add(RemotePerson(id=self.identifier, person_type='employee', embedding=[1], photo_path=str(self.photo.relative_to(self.root))))
            session.add(Employee(full_name='Local', face_embeddings=[[2]]))
            session.add(EduSchoolCatalogPerson(id='edu', person_type='employee', external_id='1'*24, full_name='Edu', source_status='active'))
            session.add(RecognitionEvent(id='history', person_id=self.identifier, person_type='employee', camera_id='c', event_type='entry', subject_signature='s'))
            session.commit()

    def test_deletes_only_legacy_and_backs_up_photos(self):
        self.assertEqual(purge_legacy(self.engine, self.root, 1, self.backup)['photo_files'], 1)
        self.assertTrue(self.photo.exists())
        purge_legacy(self.engine, self.root, 1, self.backup, apply=True)
        with Session(self.engine) as session:
            self.assertEqual(session.query(RemotePerson).count(), 0)
            self.assertEqual(session.query(Employee).count(), 1)
            self.assertEqual(session.query(EduSchoolCatalogPerson).count(), 1)
            self.assertEqual(session.query(RecognitionEvent).count(), 1)
        self.assertFalse(self.photo.exists())
        with zipfile.ZipFile(self.backup) as archive:
            self.assertEqual(archive.read(str(self.photo.relative_to(self.root))), b'test photo')

    def test_wrong_count_never_deletes(self):
        with self.assertRaises(ValueError):
            purge_legacy(self.engine, self.root, 17, self.backup, apply=True)
        self.assertTrue(self.photo.exists())

    def test_shared_photo_never_deletes(self):
        with Session(self.engine) as session:
            session.query(Employee).update({'photo_path': str(self.photo.relative_to(self.root))})
            session.commit()
        with self.assertRaises(ValueError):
            purge_legacy(self.engine, self.root, 1, self.backup, apply=True)
        self.assertTrue(self.photo.exists())

    def test_existing_backup_never_overwrites_or_deletes(self):
        self.backup.parent.mkdir(parents=True)
        self.backup.write_bytes(b'keep this backup')
        with self.assertRaises(FileExistsError):
            purge_legacy(self.engine, self.root, 1, self.backup, apply=True)
        self.assertEqual(self.backup.read_bytes(), b'keep this backup')
        self.assertTrue(self.photo.exists())
        with Session(self.engine) as session:
            self.assertEqual(session.query(RemotePerson).count(), 1)

    def test_unexpected_photo_path_is_rejected(self):
        with Session(self.engine) as session:
            session.query(RemotePerson).update({'photo_path': '../outside.jpg'})
            session.commit()
        with self.assertRaises(ValueError):
            purge_legacy(self.engine, self.root, 1, self.backup, apply=True)
        self.assertTrue(self.photo.exists())


if __name__ == '__main__':
    unittest.main()
