import io
import json
import os
import tempfile
import unittest
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from core import catalog_transfer as catalog
from database.models import Base, RemotePerson, RemotePersonReferencePhoto, Employee, AccessLogOutbox, RecognitionEvent


PID = "00000000-0000-0000-0000-000000000001"
VECTOR = [0.1] * 512
NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


class CatalogTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_root = self.root / "source"
        self.target_root = self.root / "target"
        for root in (self.source_root, self.target_root):
            models = root / "models/insightface/models/buffalo_l"
            models.mkdir(parents=True)
            for name in catalog.MODEL_FILES:
                (models / name).write_bytes(name.encode())
        self.source_engine = create_engine(f"sqlite:///{self.source_root / 'test.db'}")
        self.target_engine = self.make_target_engine()
        self.addCleanup(self.source_engine.dispose)
        self.addCleanup(self.target_engine.dispose)
        Base.metadata.create_all(self.source_engine)
        Base.metadata.create_all(self.target_engine)
        self.source = catalog.CatalogTransfer(self.source_engine, "https://erp.test", self.source_root)
        self.target = catalog.CatalogTransfer(self.target_engine, "https://erp.test", self.target_root)
        photo = self.source_root / "data/persons/main.jpg"
        photo.parent.mkdir(parents=True)
        image = Image.new("RGB", (64, 64), "green")
        exif = Image.Exif()
        exif[270] = "private-metadata"
        image.save(photo, exif=exif)
        with Session(self.source_engine) as session:
            session.add(RemotePerson(id=PID, fio="Test Person", person_type="employee", active=True,
                                     embedding=VECTOR, embedding_status="ready", photo_path="data/persons/main.jpg",
                                     photo_url="https://erp.test/photo.jpg", updated_at=NOW))
            session.flush()
            session.add(RemotePersonReferencePhoto(id=str(uuid.uuid4()), person_id=PID,
                        photo_path="data/persons/main.jpg", image_checksum=catalog.digest(b"original-upload"),
                        embedding=VECTOR, active=True, created_at=NOW))
            session.commit()

    def make_target_engine(self):
        return create_engine(f"sqlite:///{self.target_root / 'test.db'}")

    def bundle(self):
        raw, warnings = self.source.export([PID])
        return self.target.read(raw)

    def prepared(self):
        return self.target.prepare(self.bundle())

    def apply(self, prepared=None):
        prepared = prepared or self.prepared()
        preview = self.target.preview(prepared)
        return self.target.apply(prepared, preview["state"], confirmed_same_center=True)

    def rewrite(self, edit=None, extra=None):
        raw, _ = self.source.export([PID])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            contents = {n: archive.read(n) for n in archive.namelist()}
        manifest = json.loads(contents["catalog.json"])
        if edit:
            edit(manifest)
        contents["catalog.json"] = catalog.json_bytes(manifest)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, value in contents.items():
                archive.writestr(name, value)
            if extra:
                archive.writestr(*extra)
        return output.getvalue()

    def test_roundtrip_and_repeat_keep_ids_photos_and_no_erp_events(self):
        with Session(self.target_engine) as session:
            session.add(Employee(full_name="Local stays", face_embeddings=[VECTOR]))
            session.commit()
        prepared = self.prepared()
        self.assertEqual(self.apply(prepared), {"added": 1, "filled": 0, "references": 1})
        files = set(self.target_root.rglob("*.jpg"))
        self.assertEqual(self.apply(prepared), {"added": 0, "filled": 0, "references": 0})
        self.assertEqual(files, set(self.target_root.rglob("*.jpg")))
        with Session(self.target_engine) as session:
            person = session.get(RemotePerson, PID)
            self.assertEqual(person.fio, "Test Person")
            self.assertEqual(person.embedding, VECTOR)
            self.assertEqual(catalog.utc(person.updated_at), NOW)
            self.assertTrue((self.target_root / person.photo_path).is_file())
            self.assertEqual(session.query(RemotePersonReferencePhoto).count(), 1)
            self.assertEqual(session.query(Employee).one().full_name, "Local stays")
            self.assertEqual(session.query(AccessLogOutbox).count(), 0)
            self.assertEqual(session.query(RecognitionEvent).count(), 0)

    def test_fills_compact_record_without_reactivating_or_changing_timestamp(self):
        later = NOW.replace(year=2027)
        with Session(self.target_engine) as session:
            session.add(RemotePerson(id=PID, fio=None, active=False, person_type="teacher", updated_at=later))
            session.commit()
        self.apply()
        with Session(self.target_engine) as session:
            person = session.get(RemotePerson, PID)
            self.assertEqual(person.fio, "Test Person")
            self.assertFalse(person.active)
            self.assertEqual(person.person_type, "teacher")
            self.assertEqual(catalog.utc(person.updated_at), later)
            self.assertEqual(person.embedding_status, "ready")

    def test_imported_catalog_survives_compact_sync_and_remains_recognizable(self):
        from core.edge.service import EdgeService
        from core.edge.config import EdgeSettings
        self.apply()
        service = EdgeService(EdgeSettings(), engine=self.target_engine)
        with service.Session() as session:
            service._upsert_person(session, {"id": PID, "person_type": "employee", "active": True})
            session.commit()
            person = session.get(RemotePerson, PID)
            self.assertEqual(person.fio, "Test Person")
            self.assertEqual(person.embedding, VECTOR)
            self.assertTrue((self.target_root / person.photo_path).is_file())
        identities, vectors = service.cache_embeddings()
        self.assertEqual([item["person_id"] for item in identities], [PID, PID])
        self.assertEqual(vectors.shape, (2, 512))
        with service.Session() as session:
            service._upsert_person(session, {"id": PID, "person_type": "employee", "active": False, "fio": "ERP renamed"})
            session.commit()
        self.assertEqual(service.cache_embeddings(), ([], None))
        with service.Session() as session:
            self.assertEqual(session.get(RemotePerson, PID).fio, "ERP renamed")
            self.assertEqual(session.query(RemotePersonReferencePhoto).count(), 1)

    def test_reexport_target_is_read_only(self):
        self.apply()
        prepared = self.prepared()
        before = self.target.preview(prepared)["state"]
        raw, warnings = self.target.export([PID])
        self.assertEqual(self.target.read(raw).manifest["people"][0]["id"], PID)
        self.assertEqual(self.target.preview(prepared)["state"], before)

    def test_existing_fields_and_changed_official_photo_are_not_overwritten(self):
        with Session(self.target_engine) as session:
            session.add(RemotePerson(id=PID, fio="New name", active=True, person_type="employee",
                                     photo_url="https://erp.test/new.jpg", embedding=[0.2] * 512))
            session.commit()
        self.apply()
        with Session(self.target_engine) as session:
            person = session.get(RemotePerson, PID)
            self.assertEqual(person.fio, "New name")
            self.assertEqual(person.embedding, [0.2] * 512)
            self.assertEqual(person.photo_url, "https://erp.test/new.jpg")
            self.assertIsNone(person.photo_path)

    def test_stale_preview_is_rejected(self):
        prepared = self.prepared()
        preview = self.target.preview(prepared)
        with Session(self.target_engine) as session:
            session.add(RemotePerson(id=PID, fio="Concurrent sync", person_type="employee"))
            session.commit()
        with self.assertRaisesRegex(catalog.CatalogError, "Каталог изменился"):
            self.target.apply(prepared, preview["state"], True)
        self.assertEqual(list(self.target_root.rglob("*.jpg")), [])

    def test_database_failure_rolls_back_rows_and_own_photos(self):
        def fail(connection, cursor, statement, parameters, context, many):
            if statement.startswith("INSERT INTO remote_person_reference_photos"):
                raise RuntimeError("injected write failure")
        event.listen(self.target_engine, "before_cursor_execute", fail)
        try:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.apply()
        finally:
            event.remove(self.target_engine, "before_cursor_execute", fail)
        with Session(self.target_engine) as session:
            self.assertEqual(session.query(RemotePerson).count(), 0)
        self.assertEqual(list(self.target_root.rglob("*.jpg")), [])

    def test_file_write_failure_rolls_back_database(self):
        with patch.object(catalog.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply()
        with Session(self.target_engine) as session:
            self.assertEqual(session.query(RemotePerson).count(), 0)
        self.assertEqual(list(self.target_root.rglob("*.jpg")), [])

    def test_uncertain_commit_retains_media(self):
        def fail(connection):
            raise RuntimeError("uncertain commit")
        event.listen(self.target_engine, "commit", fail)
        try:
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                self.apply()
        finally:
            event.remove(self.target_engine, "commit", fail)
        self.assertTrue(list(self.target_root.rglob("*.jpg")))

    def test_reference_limits_and_inactive_references(self):
        self.target.reference_limit = 0
        prepared = self.prepared()
        self.assertEqual(self.target.preview(prepared)["rows"][0]["Пропущено по лимиту"], 1)
        self.assertEqual(self.apply(prepared)["references"], 0)
        with Session(self.source_engine) as session:
            session.query(RemotePersonReferencePhoto).one().active = False
            session.commit()
        self.assertEqual(self.apply()["references"], 1)
        with Session(self.target_engine) as session:
            self.assertFalse(session.query(RemotePersonReferencePhoto).one().active)

    def test_confirmation_and_wrong_erp_are_required(self):
        prepared = self.prepared()
        with self.assertRaises(catalog.CatalogError):
            self.target.apply(prepared, self.target.preview(prepared)["state"])
        self.target.erp_origin = "https://other.test"
        with self.assertRaises(catalog.CatalogError):
            self.bundle()

    def test_model_mismatch_requires_explicit_recompute(self):
        bundle = self.bundle()
        model = self.target_root / "models/insightface/models/buffalo_l/det_10g.onnx"
        model.write_bytes(b"different model")
        with self.assertRaises(catalog.CatalogError):
            self.target.prepare(bundle)
        with patch.object(catalog.time, "sleep"):
            prepared = self.target.prepare(bundle, True, lambda raw: [0.3] * 512)
        self.assertTrue(prepared.recomputed)
        self.apply(prepared)
        with Session(self.target_engine) as session:
            self.assertEqual(session.get(RemotePerson, PID).embedding, [0.3] * 512)

    def test_recompute_missing_face_fails_before_writes(self):
        bundle = self.bundle()
        bundle.manifest["model"]["det_10g.onnx"] = "0" * 64
        with self.assertRaises(catalog.CatalogError):
            self.target.prepare(bundle, True, lambda raw: None)
        self.assertEqual(list(self.target_root.rglob("*.jpg")), [])

    def test_missing_model_fails(self):
        (self.target_root / "models/insightface/models/buffalo_l/det_10g.onnx").unlink()
        with self.assertRaises(catalog.CatalogError):
            self.prepared()

    def test_export_is_allowlisted_strips_metadata_and_signed_urls(self):
        with Session(self.source_engine) as session:
            session.get(RemotePerson, PID).photo_url = "https://erp.test/photo?token=secret-token"
            session.commit()
        bundle = self.bundle()
        self.assertIsNone(bundle.manifest["people"][0]["photo_url"])
        manifest = catalog.json_bytes(bundle.manifest)
        self.assertNotIn(b"secret-token", manifest)
        self.assertNotIn(str(self.source_root).encode(), manifest)
        for raw in bundle.photos.values():
            with Image.open(io.BytesIO(raw)) as image:
                self.assertEqual(len(image.getexif()), 0)

    def test_missing_photo_warns_and_invalid_reference_is_skipped(self):
        with Session(self.source_engine) as session:
            session.get(RemotePerson, PID).photo_path = "data/persons/missing.jpg"
            session.query(RemotePersonReferencePhoto).one().embedding = [1]
            session.commit()
        bundle = self.bundle()
        self.assertTrue(bundle.warnings)
        self.assertEqual(bundle.photos, {})
        self.apply(self.target.prepare(bundle))

    def test_photo_outside_person_directory_is_rejected(self):
        with Session(self.source_engine) as session:
            session.get(RemotePerson, PID).photo_path = "../../private.jpg"
            session.commit()
        with self.assertRaises(catalog.CatalogError):
            self.bundle()

    def test_archive_rejects_traversal_duplicates_and_symlinks(self):
        link = zipfile.ZipInfo("photos/" + "a" * 64 + ".jpg")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        for name in ("../outside.jpg", "catalog.json", link):
            with self.subTest(name=str(name)):
                with self.assertRaises(catalog.CatalogError):
                    self.target.read(self.rewrite(extra=(name, b"bad")))

    def test_invalid_vectors_duplicate_people_and_missing_photos(self):
        edits = [lambda m: m["people"][0].update(embedding=[True] * 512),
                 lambda m: m["people"][0].update(embedding=[0.0] * 512),
                 lambda m: m["people"].append(m["people"][0].copy()),
                 lambda m: m["people"][0].update(photo="photos/" + "0" * 64 + ".jpg"),
                 lambda m: m.update(unexpected="secret")]
        for edit in edits:
            with self.subTest(edit=edit):
                with self.assertRaises(catalog.CatalogError):
                    self.target.read(self.rewrite(edit))

    def test_bad_hash_corrupt_image_and_size_limit(self):
        with self.assertRaises(catalog.CatalogError):
            self.target.read(self.rewrite(extra=("photos/" + "1" * 64 + ".jpg", b"broken")))
        with self.assertRaises(catalog.CatalogError):
            catalog.check_image(b"not an image")
        with patch.object(catalog, "MAX_ARCHIVE", 10):
            with self.assertRaises(catalog.CatalogError):
                self.target.read(b"x" * 11)
        with self.assertRaises(catalog.CatalogError):
            catalog.vector([float("nan")] * 512)
        with self.assertRaises(catalog.CatalogError):
            catalog.unique_object([("id", 1), ("id", 2)])

    def test_decoder_does_not_use_global_yolo_image_patch(self):
        with patch.object(Image, "open", side_effect=AssertionError("global decoder called")):
            self.bundle()
            for raw in (b"broken", b"\xff\xd8\xffbroken", b"\x89PNG\r\n\x1a\nbroken"):
                with self.assertRaises(catalog.CatalogError):
                    catalog.check_image(raw)


@unittest.skipUnless(os.environ.get("CATALOG_TEST_DATABASE_URL"), "isolated PostgreSQL URL not supplied")
class PostgreSQLCatalogTransferTests(CatalogTransferTests):
    def make_target_engine(self):
        # Every test gets a private schema, never the configured production DB.
        url = os.environ["CATALOG_TEST_DATABASE_URL"]
        admin = create_engine(url)
        schema = "catalog_test_" + uuid.uuid4().hex
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        def cleanup():
            with admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()
        self.addCleanup(cleanup)
        return create_engine(url, connect_args={"options": f"-csearch_path={schema}"})


class ArchiveExclusionTests(unittest.TestCase):
    def test_project_archive_excludes_catalog_exports_and_nested_caches(self):
        from tools import create_transfer_archive as archive
        for relative in ("data/exports/catalog.zip", "data/exports/report.json", "core/__pycache__/module.pyc", ".env"):
            self.assertTrue(archive.should_skip(archive.PROJECT_ROOT / relative))
        self.assertFalse(archive.should_skip(archive.PROJECT_ROOT / "core/catalog_transfer.py"))


if __name__ == "__main__":
    unittest.main()
