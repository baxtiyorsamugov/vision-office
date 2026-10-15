from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from tools.faceid_lab import choose_face, evaluate, expanded_bounds, local_child, relative_target, summarize


def face(box, vector=None):
    return SimpleNamespace(bbox=np.array(box), embedding=np.ones(512) if vector is None else vector)


def classify(vector, reason, elapsed, route, *args):
    return {"status": "unknown" if vector is not None else "no_embedding",
            "reason": reason, "ms": elapsed, "route": route}


class FaceIdLabTests(unittest.TestCase):
    def test_rejects_ambiguity_and_off_center(self):
        a = face([0, 0, 60, 60])
        self.assertEqual(choose_face([], (30, 30))[1], "no_detection")
        self.assertEqual(choose_face([a, a], (30, 30))[1], "ambiguous")
        self.assertEqual(choose_face([a], (70, 30))[1], "center_miss")

    def test_target_is_original_box_center_when_crop_clamped(self):
        center, target = relative_target([0, 0, 60, 60], [0, 0, 72, 81])
        self.assertEqual(center, (30, 30))
        candidate = face([0, 0, 35, 35])
        self.assertEqual(choose_face([candidate], center, target)[1], "usable")
        self.assertEqual(choose_face([candidate], (36, 40.5))[1], "center_miss")

    def test_overlap_and_invalid_vectors(self):
        self.assertEqual(choose_face([face([0, 0, 100, 100])], (50, 50), [45, 45, 55, 55])[1], "overlap_miss")
        for vector in (np.zeros(512), np.ones(3), np.full(512, np.nan)):
            self.assertEqual(choose_face([face([0, 0, 60, 60], vector)], (30, 30))[1], "invalid_vector")

    def test_largest_neighbour_does_not_replace_target(self):
        small = face([40, 40, 60, 60], np.full(512, 2.))
        large = face([70, 0, 200, 130])
        result, reason = choose_face([large, small], (50, 50), [40, 40, 60, 60])
        self.assertEqual(reason, "usable")
        np.testing.assert_array_equal(result, small.embedding)

    @patch("tools.faceid_lab.classify", side_effect=classify)
    def test_unknown_vector_does_not_trigger_retries(self, _):
        app = Mock()
        app.get.return_value = [face([10, 20, 70, 80])]
        other = Mock()
        other.get.return_value = []
        frame = np.zeros((200, 200, 3), dtype=np.uint8)
        result = evaluate(frame, [50, 50, 110, 110], app, other, {}, [], [])
        self.assertEqual(result["retry"]["status"], "unknown")
        self.assertEqual(result["retry"]["route"], "target320")
        self.assertEqual(app.get.call_count, 1)

    @patch("tools.faceid_lab.classify", side_effect=classify)
    def test_retry_wide_then_compact_and_original_pixels(self, _):
        frame = np.arange(200*200*3).reshape(200, 200, 3).astype(np.uint8)
        app, other = Mock(), Mock()
        app.get.return_value = []
        other.get.return_value = [face([10, 20, 70, 80])]
        result = evaluate(frame, [50, 50, 110, 110], app, other, {}, [], [])
        self.assertEqual(result["retry"]["route"], "retry160")
        self.assertEqual(result["retry"]["status"], "unknown")
        self.assertEqual(app.get.call_count, 2)
        a, b, c, d = expanded_bounds([50, 50, 110, 110], 200, 200)
        np.testing.assert_array_equal(app.get.call_args.args[0], frame[b:d, a:c])
        self.assertGreaterEqual(result["retry"]["ms"], result["compact"]["ms"])

    @patch("tools.faceid_lab.classify", side_effect=classify)
    def test_wide_success_does_not_replace_with_compact(self, _):
        app, other = Mock(), Mock()
        app.get.side_effect = [[], [face([24, 33, 84, 93])]]
        other.get.return_value = []
        result = evaluate(np.zeros((200, 200, 3)), [50, 50, 110, 110], app, other, {}, [], [], order=1)
        self.assertEqual(result["retry"]["route"], "wide320")
        self.assertEqual(result["retry"]["status"], "unknown")

    def test_summary_counts_new_lost_and_changed(self):
        unknown = {"status": "unknown", "ms": 10, "reason": "usable"}
        a = {**unknown, "status": "matched", "person_id": "a"}
        b = {**a, "person_id": "b"}
        rows = [{"outcomes": {"base": x, "target": y, "retry": y, "compact": y}}
                for x, y in [(unknown, a), (a, unknown), (a, b)]]
        summary = summarize(rows)[1]
        self.assertEqual((summary["new"], summary["lost"], summary["identity_changed"]), (1, 1, 1))
        self.assertEqual(summary["counts"]["matched"], 2)

    def test_filename_cannot_escape(self):
        for name in ("../secret.mp4", "sub/video.mp4"):
            with self.assertRaises(ValueError):
                local_child(Path.cwd(), name)

    def test_no_database_or_network_operations(self):
        import ast
        tree = ast.parse(Path("tools/faceid_lab.py").read_text(encoding="utf-8"))
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertFalse(calls & {"export_gallery", "Session", "DatabaseManager", "create_engine", "AI_Engine"})


if __name__ == "__main__":
    unittest.main()
