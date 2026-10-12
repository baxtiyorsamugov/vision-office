import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tools.audit_reference_photos import audit_photo, image_metrics, safe_photo_path, reference_manifest


class ReferenceQualityTests(unittest.TestCase):
    def test_small_dark_and_edge_flags(self):
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        result = image_metrics(image, [[0, 0, 40, 60]])
        self.assertEqual(result["face_width"], 40)
        self.assertEqual(set(result["flags"]), {"small_face", "dark", "low_detail", "clipping", "edge"})

    def test_no_face_and_multiple_faces_are_not_scored(self):
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        self.assertEqual(image_metrics(image, [])["flags"], ["no_face"])
        result = image_metrics(image, [[0, 0, 40, 60], [50, 50, 90, 90]])
        self.assertEqual(result["flags"], ["multiple_faces"])
        self.assertNotIn("sharpness", result)

    def test_invalid_box(self):
        image = np.zeros((100, 100, 3), dtype=np.uint8)
        for box in ([100, 100, 150, 150], [0, 0, float("nan"), 30]):
            self.assertEqual(image_metrics(image, [box])["flags"], ["invalid_box"])

    def test_photo_path_cannot_escape_data(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(safe_photo_path(temp, "data/a.jpg"), Path(temp).resolve() / "data/a.jpg")
            for path in ("../private.jpg", "config/settings.yaml", "data/../../private.jpg"):
                with self.assertRaises(ValueError):
                    safe_photo_path(temp, path)

    def test_audit_preserves_source_and_only_writes_preview(self):
        from PIL import Image
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "data").mkdir()
            source = root / "data/source.png"
            Image.new("RGB", (200, 200), (120, 120, 120)).save(source)
            before = source.read_bytes()
            output = root / "audit"
            (output / "media").mkdir(parents=True)
            ref = dict(id="r", person_id="p", name="Fixture", category="student", path="data/source.png")
            detector = SimpleNamespace(detect=lambda *args, **kwargs: (np.array([[20, 20, 180, 180]]), None))
            row = audit_photo(ref, root, output, 0, detector)
            self.assertTrue((output / row["thumbnail"]).is_file())
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(row["flags"], ["low_detail"])
            ref["path"] = "data/not-found.png"
            self.assertEqual(audit_photo(ref, root, output, 1, detector)["flags"], ["unreadable"])

    def test_missing_database_is_not_created(self):
        from core.offline_video import readonly_session
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "absent.db"
            with patch("database.manager.database_url", return_value=f"sqlite:///{path.as_posix()}"):
                with self.assertRaises(FileNotFoundError), readonly_session():
                    pass
            self.assertFalse(path.exists())

    def test_manifest_readonly_and_active_scope(self):
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import Session
        from database.models import Base, EduSchoolCatalogPerson as Person, EduSchoolReferencePhoto as Photo
        from core.offline_video import readonly_session

        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "test.db"
            url = f"sqlite:///{db.as_posix()}"
            engine = create_engine(url)
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                for i, active, photo_active in ((1, True, True), (2, True, False), (3, False, True)):
                    session.add(Person(id=str(i), external_id=str(i), full_name="Fixture", person_type="student",
                                       active=active, source_status="active"))
                    session.add(Photo(id=str(i), person_id=str(i), photo_path="data/a.jpg", active=photo_active,
                                      image_checksum=str(i), embedding=[1.] * 512))
                session.commit()
            engine.dispose()
            before = hashlib.sha256(db.read_bytes()).digest()
            with patch("database.manager.database_url", return_value=url):
                rows, coverage = reference_manifest()
                self.assertEqual([r["id"] for r in rows], ["1"])
                self.assertEqual(coverage, {"active": {"student": 2}, "without_reference": {"student": 1}})
                self.assertNotIn("embedding", rows[0])
                with readonly_session() as Factory, Factory() as session:
                    with self.assertRaises(Exception):
                        session.execute(text("DELETE FROM eduschool_reference_photos"))
            self.assertEqual(before, hashlib.sha256(db.read_bytes()).digest())


if __name__ == "__main__":
    unittest.main()
