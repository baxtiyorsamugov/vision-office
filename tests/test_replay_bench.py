import importlib.util
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_config = load("replay_make_config", "tools/replay/make_config.py")
summary = load("replay_summary", "tools/replay/summary.py")


class ReplayConfigTests(unittest.TestCase):
    def test_recordings_become_isolated_rtsp_cameras_and_senders_are_off(self):
        production = {
            "cameras": [{"id": "reception_01", "rtsp_url": "rtsp://user:secret@10.0.0.5/1"}],
            "edge_integration": {"enabled": True, "recognition_threshold": 0.40},
            "eduschool_catalog": {"enabled": True, "recognition_threshold": 0.50},
            "eduschool_turnstile": {"enabled": True, "students_enabled": True, "device_ids": {"reception_01": "G1"}},
            "unknown_visitors": {"enabled": False},
            "ai": {"face_detection_imgsz": 960},
        }
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            for name in ("kirish.mp4", "chiqish1.MP4", "bad$name.mp4", "notes.txt"):
                (directory / name).write_bytes(b"")
            recordings = make_config.find_recordings(directory)
        self.assertEqual([p.name for p in recordings], ["chiqish1.MP4", "kirish.mp4"])
        settings = make_config.build_settings(production, recordings)
        cameras = {c["id"]: c for c in settings["cameras"]}
        self.assertEqual(set(cameras), {"replay_kirish", "replay_chiqish1"})
        self.assertEqual(cameras["replay_kirish"]["rtsp_url"], "rtsp://replay-rtsp:8554/kirish")
        self.assertEqual(cameras["replay_chiqish1"]["event_type"], "exit")
        self.assertNotIn("secret", str(settings["cameras"]))
        self.assertFalse(settings["eduschool_turnstile"]["enabled"])
        self.assertFalse(settings["eduschool_turnstile"]["students_enabled"])
        self.assertEqual(settings["eduschool_turnstile"]["device_ids"], {})
        self.assertTrue(settings["unknown_visitors"]["enabled"])
        # Recognition settings are copied unchanged.
        self.assertEqual(settings["eduschool_catalog"]["recognition_threshold"], 0.50)
        self.assertEqual(settings["edge_integration"]["recognition_threshold"], 0.40)
        self.assertEqual(settings["ai"], production["ai"])
        pinned = make_config.build_settings({"eduschool_catalog": {}, "health": {"telegram_enabled": True}}, recordings,
                                            edge_threshold=0.40, eduschool_threshold=0.50)
        self.assertEqual(pinned["eduschool_catalog"]["recognition_threshold"], 0.50)
        self.assertEqual(pinned["edge_integration"]["recognition_threshold"], 0.40)
        self.assertFalse(pinned["health"]["telegram_enabled"])
        # Production dict is not mutated.
        self.assertTrue(production["eduschool_turnstile"]["enabled"])


class ReplaySummaryTests(unittest.TestCase):
    def test_classification_bands(self):
        self.assertEqual(summary.classify(None, 0.5), "no-embedding")
        self.assertEqual(summary.classify(0.47, 0.5), "near-miss")
        self.assertEqual(summary.classify(0.35, 0.5), "weak")
        self.assertEqual(summary.classify(0.10, 0.5), "far")
        self.assertEqual(summary.threshold_for("edu:1", 0.40, 0.50), 0.50)
        self.assertEqual(summary.threshold_for("abc", 0.40, 0.50), 0.40)

    def test_top_candidates_are_unique_people(self):
        names = [{"name": "A", "person_id": "edu:a"}, {"name": "A", "person_id": "edu:a"}, {"name": "B", "person_id": "edu:b"}]
        vectors = np.eye(3, 4, dtype=np.float32)
        vectors[1] = [0.9, 0.1, 0, 0]
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        result = summary.top_candidates([1, 0, 0, 0], names, vectors)
        self.assertEqual([c["name"] for c in result], ["A", "B"])
        self.assertEqual(result[0]["score"], 1.0)

    def test_render_writes_report(self):
        events = [
            {"id": "1", "camera_id": "replay_kirish", "created_at": "2026-11-04T08:00:00+00:00", "known": True,
             "person_name": "A <b>", "confidence": 0.61, "photo_path": "data/events/2026-11-04/1.jpg",
             "photo_size": (90, 110), "candidates": [], "threshold": 0.5, "category": "recognised"},
            {"id": "2", "camera_id": "replay_kirish", "created_at": "2026-11-04T08:00:01+00:00", "known": False,
             "person_name": None, "confidence": 0.46, "photo_path": None, "photo_size": None,
             "candidates": [{"name": "B", "person_id": "edu:b", "score": 0.46}], "threshold": 0.5, "category": "near-miss"},
        ]
        with tempfile.TemporaryDirectory() as folder:
            from datetime import datetime, timezone
            path = summary.render(events, Path(folder) / "r", datetime(2026, 11, 4, tzinfo=timezone.utc))
            page = path.read_text(encoding="utf-8")
        self.assertIn("../../events/2026-11-04/1.jpg", page)
        self.assertIn("A &lt;b&gt;", page)
        self.assertIn("near-miss", page)


class ReplayPeopleSummaryTests(unittest.TestCase):
    @staticmethod
    def event(category, known, person_id, name, score, margin=None):
        candidates = [] if known else [{"name": name, "person_id": person_id, "score": score},
                                       {"name": "Other", "person_id": "other", "score": score - (margin or 0.1)}]
        return {"id": "e", "camera_id": "replay_kirish", "created_at": "2026-11-04T08:00:00", "known": known,
                "person_name": name if known else None, "confidence": score if known else None, "photo_path": None,
                "photo_size": None, "candidates": candidates, "margin": None if known else margin,
                "best_person_id": person_id, "best_name": name, "threshold": 0.5, "category": category}

    def test_people_are_counted_once_and_far_faces_are_not_attributed(self):
        events = [
            self.event("near-miss", False, "edu:1", "Xasanov", 0.49, 0.08),
            self.event("near-miss", False, "edu:1", "Xasanov", 0.47, 0.05),
            self.event("recognised", True, "edu:2", "Ali", 0.60),
            self.event("near-miss", False, "edu:2", "Ali", 0.46, 0.10),
            self.event("far", False, "edu:9", "Noise", 0.20, 0.01),
        ]
        people = {p["person_id"]: p for p in summary.people_summary(events)}
        self.assertEqual(set(people), {"edu:1", "edu:2"})
        self.assertEqual(people["edu:1"]["best_category"], "near-miss")
        self.assertEqual(people["edu:1"]["observations"], 2)
        self.assertAlmostEqual(people["edu:1"]["min_margin"], 0.05)
        self.assertEqual(people["edu:2"]["best_category"], "recognised")


if __name__ == "__main__":
    unittest.main()
