import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.build_photo_review import build, digest, event_person_id, photo_path, render, snapshot_id


class PhotoReviewTests(unittest.TestCase):
    def test_paths_never_resolve_into_live_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for value in ("data/events/day/a.jpg", "/app/data/events/day/a.jpg", "data\\events\\day\\a.jpg"):
                self.assertEqual(photo_path(root, value), root / "events/day/a.jpg")
            for bad in ("data/../live.jpg", "data/events/../../secret.jpg", "C:/live/data/events/a.jpg",
                        "https://server/face.jpg", "data/faces/a.jpg", "data/events/C:/secret.jpg", None):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    photo_path(root, bad)

    def test_system_identity_is_metadata_not_a_decision(self):
        self.assertEqual(event_person_id({"person_type": "eduschool_student", "person_id": "edu:s:123"}), "student:123")
        self.assertEqual(event_person_id({"person_type": "eduschool_employee", "person_id": "edu:e:123"}), "employee:123")
        self.assertIsNone(event_person_id({"person_type": "employee", "person_id": "123"}))
        self.assertIsNone(event_person_id({"person_type": "eduschool_student", "person_id": "edu:e:123"}))

    def test_fingerprint_tracks_catalog_and_photo_changes(self):
        data = {"version": 1, "dump_sha256": "dump", "people": [], "events": [{"id": "one", "sha256": "a"}]}
        before = snapshot_id(data)
        data["events"][0]["sha256"] = "b"
        self.assertNotEqual(before, snapshot_id(data))
        before = snapshot_id(data)
        data["people"].append({"id": "person", "active": False})
        self.assertNotEqual(before, snapshot_id(data))

    def test_unchanged_imported_files_and_missing_photos(self):
        from contextlib import contextmanager
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, audit, output = root / "input", root / "audit", root / "out"
            (source / "events/day").mkdir(parents=True)
            audit.mkdir()
            dump = source / "source.dump"
            dump.write_bytes(b"fixture dump")
            image = source / "events/day/a.jpg"
            image.write_bytes(b"audited fixture image")
            (audit / "summary.json").write_text(json.dumps({"dump": {"sha256": digest(dump)}}))
            (audit / "details.json").write_text(json.dumps({"images": [{"path": "events/day/a.jpg", "status": "ok",
                "sha256": digest(image), "width": 120, "height": 150}]}))
            person = {"id": "student:1", "full_name": "Person", "active": True, "person_type": "student"}
            event = {"id": "event", "camera_id": "entry", "event_type": "entry", "person_id": "edu:s:1",
                "person_type": "eduschool_student", "person_name": "Person", "photo_path": "data/events/day/a.jpg", "created_at": "2026-10-12T00:00:00Z"}

            @contextmanager
            def isolated(*_):
                def query(sql):
                    if "eduschool_catalog_people" in sql:
                        return [dict(person)]
                    if "eduschool_reference_photos" in sql:
                        return []
                    return [event, {**event, "id": "missing", "photo_path": "data/events/day/missing.jpg"}]
                yield query

            with patch("tools.build_photo_review.isolated_dump", isolated):
                build(source, audit, output)
            result = json.loads((output / "manifest.json").read_text())
            self.assertEqual(len(result["events"]), 1)
            self.assertEqual(result["skipped"], {"events": 1})
            self.assertEqual(result["events"][0]["prediction_person_id"], "student:1")
            self.assertNotIn("decisions", result)
            self.assertNotIn("embedding", json.dumps(result))
            self.assertEqual((output / result["events"][0]["image"]).read_bytes(), image.read_bytes())
            image.write_bytes(b"changed")
            with patch("tools.build_photo_review.isolated_dump", isolated), self.assertRaisesRegex(ValueError, "changed"):
                build(source, audit, root / "other")

    def test_render_escapes_untrusted_data_and_checks_snapshot(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            report = {"version": 1, "dump_sha256": "hash", "people": [{"full_name": "</script><script>bad()</script>"}], "events": []}
            report["snapshot_id"] = snapshot_id(report)
            (output / "manifest.json").write_text(json.dumps(report))
            render(output)
            html = (output / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>bad()", html)
            self.assertIn("\\u003c/script>", html)
            self.assertIn("connect-src 'none'", html)
            self.assertTrue((output / "icons/check.svg").is_file())
            report["snapshot_id"] = "tampered"
            (output / "manifest.json").write_text(json.dumps(report))
            with self.assertRaises(ValueError):
                render(output)


if __name__ == "__main__":
    unittest.main()
