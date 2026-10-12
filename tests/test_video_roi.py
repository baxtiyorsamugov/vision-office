from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from tools.compare_video_roi import analyze_variant, scoped_boxes, validate_preview, validate_roi


class VideoRoiTests(unittest.TestCase):
    def test_roi_validation(self):
        self.assertEqual(validate_roi([20, 10, 90, 100], 100, 100), [20, 10, 90, 100])
        for box in ([0, 0, 101, 100], [-1, 0, 50, 50], [20, 20, 20, 50]):
            with self.assertRaises(ValueError):
                validate_roi(box, 100, 100)

    def test_same_scope_and_boundary_exclusion(self):
        roi = [100, 200, 300, 400]
        full = [[120, 230, 200, 310], [10, 20, 40, 50], [100, 200, 150, 250]]
        crop = [[20, 30, 100, 110], [0, 0, 50, 50]]
        self.assertEqual(scoped_boxes(full, roi, False), [[120, 230, 200, 310]])
        self.assertEqual(scoped_boxes(full, roi, False), scoped_boxes(crop, roi, True))

    def test_original_pixels_and_minimum_size_are_preserved(self):
        frame = np.arange(200 * 300 * 3, dtype=np.uint32).reshape((200, 300, 3)).astype(np.uint8)
        seen = []
        class Boxes:
            def __init__(self, values):
                self.values = values
                self.xyxy = self
            def cpu(self):
                return self
            def numpy(self):
                return np.asarray(self.values)
        def predict(image, **kwargs):
            values = [[10, 10, 70, 70], [90, 10, 130, 50]] if image.shape[1] == 160 else [[110, 30, 170, 90], [190, 30, 230, 70]]
            return [SimpleNamespace(boxes=Boxes(values))]
        def match(image, *args):
            seen.append(image.copy())
            return {"status": "unknown"}, 1
        with patch("tools.compare_video_roi.match_crop", side_effect=match):
            args = (SimpleNamespace(predict=predict), None, {"threshold": .4, "eduschool_threshold": .55}, [], [], "cpu", 960)
            a = analyze_variant(frame, [100, 20, 260, 180], False, *args)[0]
            b = analyze_variant(frame, [100, 20, 260, 180], True, *args)[0]
        self.assertEqual([f["status"] for f in a], ["unknown", "small"])
        self.assertEqual([f["status"] for f in a], [f["status"] for f in b])
        self.assertEqual(a[0]["box"], [110, 30, 170, 90])
        self.assertEqual(b[0]["box"], [10, 10, 70, 70])
        np.testing.assert_array_equal(seen[0], seen[1])

    def test_preview_timing_and_aspect(self):
        source = {"frames": 100, "fps": 25}
        preview = {"frames": 100, "fps": 25, "width": 1280, "height": 720}
        validate_preview(source, preview, 16/9)
        for field, value in (("frames", 90), ("fps", 30), ("width", 640)):
            with self.assertRaises(ValueError):
                validate_preview(source, {**preview, field: value}, 16/9)

    def test_no_database_or_delivery_services(self):
        source = Path("tools/compare_video_roi.py").read_text(encoding="utf-8")
        for name in ("database.manager", "RecognitionEventStore", "EdgeService", "requests", "httpx", "readonly_session"):
            self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()
