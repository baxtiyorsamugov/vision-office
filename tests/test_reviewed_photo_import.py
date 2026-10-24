import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from database.models import Base, EduSchoolCatalogPerson as Person, EduSchoolReferencePhoto as Photo
from tools.import_reviewed_photos import open_engine, preflight, run_import, validate_bundle


class ReviewedImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'copy.db'
        self.url = 'sqlite:///' + self.db.as_posix()
        self.engine = create_engine(self.url)
        Base.metadata.create_all(self.engine)
        self.pid = 'employee:' + '1' * 24
        with Session(self.engine) as session:
            session.add(Person(id=self.pid, external_id='1' * 24, full_name='Fixture Person',
                               person_type='employee', active=True, source_status='active'))
            session.commit()
        self.bundle = self.root / 'input'
        (self.bundle / 'photos').mkdir(parents=True)
        self.items, decisions = [], {}
        for i in range(2):
            buf = io.BytesIO()
            Image.new('RGB', (120, 120), (60+80*i, 50, 40)).save(buf, format='JPEG')
            raw = buf.getvalue(); checksum = hashlib.sha256(raw).hexdigest()
            decision = dict(status='confirmed', reference_approved=True, human_confirmed=True,
                            reason=None, person_id=self.pid, image_sha256=checksum)
            decisions[str(i)] = decision
            item = dict(event_id=str(i), person_id=self.pid, person_type='employee', full_name='Fixture Person',
                        sha256=checksum, human_decision=decision, image=f'photos/{checksum}.jpg', width=120, height=120)
            self.items.append(item); (self.bundle / item['image']).write_bytes(raw)
        review = dict(version=1, purpose='manual_photo_review', applied=False, snapshot_id='snapshot',
                      dump_sha256='dump', decisions=decisions)
        raw = json.dumps(review).encode()
        (self.bundle / 'original-human-review.json').write_bytes(raw)
        self.manifest = dict(version=1, purpose='human_selected_reference_candidates', applied=False,
                             embeddings_computed=False, source_snapshot_id='snapshot', source_dump_sha256='dump',
                             source_annotation_sha256=hashlib.sha256(raw).hexdigest(), items=self.items)
        self.save_manifest()

    def save_manifest(self):
        (self.bundle / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def tearDown(self):
        self.engine.dispose(); self.temp.cleanup()

    def count(self):
        with Session(self.engine) as session:
            return len(list(session.scalars(select(Photo))))

    def run_copy(self, **options):
        return run_import(self.engine, self.bundle, self.root, self.root / options.pop('output', 'result'),
                          expected_count=2, **options)

    def test_dry_run_is_read_only_and_does_not_invoke_processing(self):
        before = self.db.read_bytes()
        readonly = open_engine(self.url, readonly=True)
        try:
            with readonly.connect() as connection:
                with self.assertRaises(Exception):
                    connection.exec_driver_sql('DELETE FROM employees')
            report = run_import(readonly, self.bundle, self.root, self.root / 'preview', expected_count=2,
                                embedding_fn=lambda _: self.fail('Dry run invoked model'))
        finally:
            readonly.dispose()
        self.assertEqual(report['counts'], {'ready': 2})
        self.assertFalse(report['face_processing_tested'])
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse((self.root / 'data').exists())

    def test_apply_backup_and_idempotent_retry_using_synthetic_vectors(self):
        report = self.run_copy(apply=True, embedding_fn=lambda _: np.ones(512))
        self.assertEqual(report['counts'], {'imported': 2})
        self.assertEqual(self.count(), 2)
        with zipfile.ZipFile(report['backup']['path']) as archive:
            records = json.loads(archive.read('records.json'))
            self.assertEqual(len(records['people']), 1)
            self.assertEqual(records['references'], [])
            self.assertIsNone(archive.testzip())
        again = self.run_copy(apply=True, output='retry', embedding_fn=lambda _: self.fail('Duplicate processed'))
        self.assertEqual(again['counts'], {'already_present': 2})
        self.assertEqual(self.count(), 2)
        self.assertFalse((self.root / 'data/.reviewed-photo-import.lock').exists())

    def test_tampered_photo_blocks_before_any_output(self):
        (self.bundle / self.items[0]['image']).write_bytes(b'wrong')
        with self.assertRaises(ValueError): self.run_copy(apply=True)
        self.assertFalse((self.root / 'result').exists())
        self.assertEqual(self.count(), 0)

    def test_wrong_approval_or_path_rejected(self):
        self.items[0]['human_decision']['human_confirmed'] = False
        self.save_manifest()
        with self.assertRaises(ValueError): validate_bundle(self.bundle)
        self.items[0]['human_decision']['human_confirmed'] = True
        self.items[0]['image'] = '../outside.jpg'
        self.save_manifest()
        with self.assertRaises(ValueError): validate_bundle(self.bundle)

    def test_missing_and_changed_profile_block_whole_package(self):
        with Session(self.engine) as session:
            session.get(Person, self.pid).full_name = 'Changed Name'; session.commit()
        result = self.run_copy(apply=True)
        self.assertEqual(result['counts'], {'blocked': 2})
        self.assertIsNone(result['backup'])
        self.assertEqual(self.count(), 0)

    def test_inactive_profile_is_skipped_only_when_requested(self):
        with Session(self.engine) as session:
            session.get(Person, self.pid).active = False; session.commit()
        self.assertEqual(self.run_copy(apply=True)['counts'], {'blocked': 2})
        result = self.run_copy(apply=True, output='skip', skip_inactive=True,
                               embedding_fn=lambda _: self.fail('Skipped photo processed'))
        self.assertEqual(result['counts'], {'skipped_inactive': 2})
        self.assertEqual(self.count(), 0)

    def test_over_limit_photos_are_skipped_in_manifest_order_only_when_requested(self):
        self.assertEqual(self.run_copy(reference_limit=1)['counts'], {'ready': 1, 'blocked': 1})
        result = self.run_copy(apply=True, output='skip', reference_limit=1, skip_over_limit=True,
                               embedding_fn=lambda _: np.ones(512))
        self.assertEqual([r['status'] for r in result['results']], ['imported', 'skipped_limit'])
        self.assertEqual(self.count(), 1)

    def test_aggregate_limit_blocks_before_apply(self):
        result = self.run_copy(apply=True, reference_limit=1)
        self.assertEqual(result['counts'], {'ready': 1, 'blocked': 1})
        self.assertEqual(self.count(), 0)

    def test_backup_failure_prevents_model_and_writes(self):
        with patch('tools.import_reviewed_photos.backup_references', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                self.run_copy(apply=True, embedding_fn=lambda _: self.fail('No backup'))
        self.assertEqual(self.count(), 0)
        self.assertFalse((self.root / 'data/.reviewed-photo-import.lock').exists())

    def test_unusable_face_is_rejected_not_forced(self):
        result = self.run_copy(apply=True, embedding_fn=lambda _: None)
        self.assertEqual(result['counts'], {'rejected': 2})
        self.assertEqual(self.count(), 0)

    def test_existing_inactive_photo_is_not_reactivated(self):
        self.run_copy(apply=True, embedding_fn=lambda _: np.ones(512))
        with Session(self.engine) as session:
            for p in session.scalars(select(Photo)): p.active = False
            session.commit()
        result = self.run_copy(apply=True, output='retry')
        self.assertEqual(result['counts'], {'already_present_inactive': 2})
        with Session(self.engine) as session:
            self.assertFalse(any(p.active for p in session.scalars(select(Photo))))

    def test_legacy_only_database_is_not_migrated(self):
        path = self.root / 'old.db'
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE employees(id INTEGER)')
        before = path.read_bytes()
        engine = open_engine('sqlite:///' + path.as_posix(), readonly=True)
        try:
            with self.assertRaises(ValueError): preflight(engine, self.manifest, 10)
        finally: engine.dispose()
        self.assertEqual(path.read_bytes(), before)

    def test_existing_reference_files_are_backed_up_and_unchanged(self):
        old_file = self.root / 'data/persons/eduschool/fixture/old.jpg'
        old_file.parent.mkdir(parents=True)
        old_raw = b'previous-reference-bytes'
        old_file.write_bytes(old_raw)
        with Session(self.engine) as session:
            session.add(Photo(id='original', person_id=self.pid, photo_path=old_file.relative_to(self.root).as_posix(),
                              image_checksum='original', embedding=[1.0]*512, active=True, source='eduschool'))
            session.commit()
        with patch('core.eduschool.photos.run_migrations', side_effect=AssertionError('No migrations')):
            result = self.run_copy(apply=True, embedding_fn=lambda _: np.ones(512))
        self.assertEqual(result['counts'], {'imported':2})
        self.assertEqual(old_file.read_bytes(), old_raw)
        with zipfile.ZipFile(result['backup']['path']) as archive:
            self.assertEqual(archive.read(old_file.relative_to(self.root).as_posix()),old_raw)
            self.assertEqual(len(json.loads(archive.read('records.json'))['references']),1)
        with Session(self.engine) as session:
            original = session.get(Photo,'original')
            self.assertTrue(original.active)
            self.assertEqual(original.embedding,[1.0]*512)

    def test_unexpected_failure_is_reported_and_retry_is_idempotent(self):
        calls = []
        def fail_second(_):
            calls.append(1)
            if len(calls)==2: raise RuntimeError('simulated interruption')
            return np.ones(512)
        with self.assertRaises(RuntimeError): self.run_copy(apply=True,embedding_fn=fail_second)
        partial=json.loads((self.root/'result/report.json').read_text(encoding='utf-8'))
        self.assertFalse(partial['complete'])
        self.assertEqual(partial['counts'],{'imported':1,'processing':1})
        self.assertEqual(self.count(),1)
        result=self.run_copy(apply=True,output='retry',embedding_fn=lambda _:np.ones(512))
        self.assertEqual(result['counts'],{'already_present':1,'imported':1})
        self.assertEqual(self.count(),2)

    def test_exclusive_lock_preserves_existing_lock(self):
        lock=self.root/'data/.reviewed-photo-import.lock'
        lock.parent.mkdir(parents=True);lock.write_text('other import',encoding='utf-8')
        with self.assertRaises(FileExistsError): self.run_copy(apply=True)
        self.assertEqual(lock.read_text(encoding='utf-8'),'other import')
        self.assertEqual(self.count(),0)

    def test_service_identity_guard_is_not_bypassed_for_synthetic_fixture(self):
        from database.models import RemotePerson
        with Session(self.engine) as session:
            session.add(RemotePerson(id='fixture-remote',person_type='employee',active=True,embedding=[1.0]*512))
            session.commit()
        result=self.run_copy(apply=True,embedding_fn=lambda _:np.ones(512))
        self.assertEqual(result['counts'],{'rejected':2})
        self.assertEqual(self.count(),0)



class ReferenceLimitDefaultsTests(unittest.TestCase):
    def test_default_allows_fifteen_local_references_and_config_still_overrides(self):
        from core.edge.config import load_edge_settings
        from core.eduschool.catalog import load_settings
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / 'absent.yaml'
            self.assertEqual(load_settings(missing).local_reference_photo_limit, 15)
            self.assertEqual(load_edge_settings(missing).local_reference_photo_limit, 15)
            custom = Path(folder) / 'settings.yaml'
            custom.write_text('eduschool_catalog:\n  local_reference_photo_limit: 10\n', encoding='utf-8')
            self.assertEqual(load_settings(custom).local_reference_photo_limit, 10)


if __name__ == '__main__':
    unittest.main()
