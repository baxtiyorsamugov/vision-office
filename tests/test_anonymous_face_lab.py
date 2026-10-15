from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np

from tools.anonymous_face_lab import extract, initial_reference, source_asset


class AnonymousFaceLabTests(unittest.TestCase):
    def test_reference_and_probe_use_same_normalized_vector(self):
        face = SimpleNamespace(bbox=np.array([10, 10, 100, 120]), embedding=np.ones(512))
        app = SimpleNamespace(get=lambda crop: [face])
        row = extract(np.zeros((220, 220, 3), dtype=np.uint8), [50, 50, 130, 140], app)
        self.assertTrue(row["reference_ok"])
        self.assertAlmostEqual(np.linalg.norm(row["vector"]), 1, places=6)

    def test_references_exclude_edges_small_and_multiple_faces(self):
        face = SimpleNamespace(bbox=np.array([0, 0, 150, 150]), embedding=np.ones(512))
        neighbour = SimpleNamespace(bbox=np.array([151, 0, 190, 60]), embedding=np.ones(512))
        frame = np.zeros((220, 220, 3), dtype=np.uint8)
        for box, faces in [([0, 0, 80, 80], [face]), ([50, 50, 105, 105], [face]),
                           ([50, 50, 130, 130], [face, neighbour])]:
            with self.subTest(box=box, faces=len(faces)):
                row = extract(frame, box, SimpleNamespace(get=lambda crop: faces))
                self.assertIsNotNone(row["vector"])
                self.assertFalse(row["reference_ok"])

    def test_bad_embeddings_and_undersized_probes(self):
        frame = np.zeros((220, 220, 3), dtype=np.uint8)
        row = extract(frame, [50, 50, 130, 130], SimpleNamespace(get=lambda crop: []))
        self.assertIsNone(row["vector"])
        self.assertFalse(row["reference_ok"])
        with self.assertRaises(ValueError):
            extract(frame, [0, 0, 40, 40], None)

    def test_seed_selection_uses_size_not_held_out_matches(self):
        rows = [{"id": "one", "box": [0, 0, 70, 70], "reference_ok": True, "sharpness": 200},
                {"id": "two", "box": [0, 0, 80, 80], "reference_ok": True, "sharpness": 100},
                {"id": "bad", "box": [0, 0, 150, 150], "reference_ok": False, "sharpness": 300}]
        self.assertEqual(initial_reference(rows), "two")
        self.assertIsNone(initial_reference([]))

    def test_asset_cannot_escape_or_overwrite_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "media").mkdir()
            (root / "media/test.jpg").touch()
            self.assertEqual(source_asset(root, "media/test.jpg"), root / "media/test.jpg")
            for path in ("../test.jpg", "media/../media/test.jpg", str(root / "media/test.jpg"), "media/missing.jpg"):
                with self.assertRaises(ValueError):
                    source_asset(root, path)

    def test_no_live_database_or_delivery_calls(self):
        source = Path("tools/anonymous_face_lab.py").read_text(encoding="utf-8")
        for token in ("export_gallery(", "Session(", "create_engine(", "AI_Engine(", "requests.", "httpx.", "RecognitionEventStore"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
