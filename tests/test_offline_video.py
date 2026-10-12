import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.offline_video import crop_bounds, latency_summary, sample_due, write_report


class OfflineVideoTests(unittest.TestCase):
    def test_sampling_uses_source_time_without_drift(self):
        chosen = []
        for frame in range(1250):
            if sample_due(frame, 25, 5, len(chosen)):
                chosen.append(frame)
        self.assertEqual(chosen, list(range(0, 1250, 5)))

    def test_non_integer_frame_rate(self):
        chosen = []
        for frame in range(1350):
            if sample_due(frame, 25.155123, 5, len(chosen)):
                chosen.append(frame)
        for i, frame in enumerate(chosen):
            self.assertGreaterEqual(frame / 25.155123 + 1e-8, i / 5)
            self.assertLess(frame / 25.155123 - i / 5, 1 / 25.155123 + 1e-8)

    def test_crop_preserves_production_padding_and_clamps(self):
        self.assertEqual(crop_bounds([100, 100, 200, 200], 300, 300), (80, 65, 220, 235))
        self.assertEqual(crop_bounds([0, 0, 100, 100], 100, 100), (0, 0, 100, 100))

    def test_latency_empty_and_percentiles(self):
        self.assertIsNone(latency_summary([]))
        self.assertEqual(latency_summary([10, 30, 20]), {"p50": 20., "p95": 29.})

    def test_embedded_data_cannot_close_script(self):
        with tempfile.TemporaryDirectory() as temp:
            template = Path(temp) / "template.html"
            result = Path(temp) / "index.html"
            template.write_text('<script type="application/json">__REPORT_JSON__</script>', encoding="utf-8")
            payload = {"name": "</script><script>alert(1)</script>"}
            write_report(payload, result, template)
            html = result.read_text(encoding="utf-8")
            self.assertEqual(html.count("</script>"), 1)
            self.assertEqual(json.loads(html.split('>', 1)[1].split('</script>')[0]), payload)

    def test_analyzer_does_not_invoke_attendance_or_network_clients(self):
        import ast
        source = Path("tools/analyze_videos.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = {"RecognitionEventStore", "EdgeService", "EduSchoolPhotoService", "AI_Engine", "requests", "httpx"}
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertFalse(names & forbidden)

    def test_readonly_snapshot_preserves_database(self):
        import hashlib
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from database.models import Base, Employee, EduSchoolCatalogPerson, EduSchoolReferencePhoto
        from core.offline_video import export_gallery

        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "office.db"
            engine = create_engine(f"sqlite:///{db.as_posix()}")
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                session.add(Employee(full_name="Local fixture", face_embeddings=[[1.] * 512]))
                for active, name in ((True, "a"), (False, "b")):
                    person_id = "student:" + name * 24
                    session.add(EduSchoolCatalogPerson(id=person_id, person_type="student", external_id=name * 24,
                                full_name="Student fixture", active=active, source_status="active"))
                    session.add(EduSchoolReferencePhoto(id=name, person_id=person_id, photo_path="fixture.jpg",
                                image_checksum=name, embedding=[1.] * 512, active=True))
                session.commit()
            engine.dispose()
            before = hashlib.sha256(db.read_bytes()).digest()
            with patch("database.manager.database_url", return_value=f"sqlite:///{db.as_posix()}"), \
                 patch("core.edge.config.load_edge_settings", return_value=SimpleNamespace(recognition_threshold=.4)), \
                 patch("core.eduschool.catalog.load_settings", return_value=SimpleNamespace(recognition_threshold=.55)):
                summary = export_gallery(Path(temp) / "gallery.json")
            self.assertEqual(summary["people"], 2)
            self.assertEqual(summary["active_catalog"], {"student": 1})
            self.assertEqual(hashlib.sha256(db.read_bytes()).digest(), before)

    def test_known_unknown_no_embedding_and_no_gallery(self):
        import numpy as np
        from tools.analyze_videos import match_crop
        recognizer = SimpleNamespace(get_embedding=lambda *a, **kw: np.ones(512, dtype=np.float32))
        names = [{"person_id": "edu:s:" + "a" * 24, "person_type": "eduschool_student", "name": "Fixture"}]
        vectors = np.ones((1, 512), dtype=np.float32) / np.sqrt(512)
        result, _ = match_crop(None, recognizer, names, vectors, .4, .55)
        self.assertEqual(result["status"], "matched")
        result, _ = match_crop(None, recognizer, names, -vectors, .4, .55)
        self.assertEqual(result, {"status": "unknown"})
        result, _ = match_crop(None, recognizer, [], [], .4, .55)
        self.assertEqual(result["status"], "no_gallery")
        recognizer.get_embedding = lambda *a, **kw: None
        result, _ = match_crop(None, recognizer, names, vectors, .4, .55)
        self.assertEqual(result["status"], "no_embedding")

    def test_short_video_end_to_end_with_fake_models(self):
        import cv2
        import numpy as np
        from tools.analyze_videos import analyze_file

        class Boxes:
            xyxy = None

            def cpu(self):
                return self

            def numpy(self):
                return np.array([[2, 2, 62, 62], [2, 2, 20, 20]])

        boxes = Boxes()
        boxes.xyxy = boxes
        model = SimpleNamespace(predict=lambda *a, **kw: [SimpleNamespace(boxes=boxes)])
        recognizer = SimpleNamespace(using_cuda=False, get_embedding=lambda *a, **kw: np.ones(512))
        names = [{"person_id": "local:1", "person_type": "local_employee", "name": "Fixture"}]
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.avi"
            writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"MJPG"), 25, (64, 64))
            self.assertTrue(writer.isOpened())
            for _ in range(10):
                writer.write(np.zeros((64, 64, 3), dtype=np.uint8))
            writer.release()
            output = Path(temp) / "report"
            (output / "media").mkdir(parents=True)
            args = SimpleNamespace(sample_fps=5, yolo_device="cpu", imgsz=640, ffmpeg=None)
            result = analyze_file(source, 0, output, model, recognizer,
                                  {"threshold": .4, "eduschool_threshold": .55}, names,
                                  np.ones((1, 512), dtype=np.float32) / np.sqrt(512), args)
            self.assertEqual(result["decoded_frames"], 10)
            self.assertEqual(len(result["samples"]), 2)
            self.assertEqual(result["counts"], {"matched": 2, "small": 2})
            self.assertTrue((output / result["video"]).is_file())
            self.assertTrue((output / result["samples"][0]["image"]).is_file())


if __name__ == "__main__":
    unittest.main()
