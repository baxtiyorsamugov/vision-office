import os
import threading
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from core.ai.runtime import limit_torch_threads, torch_device, wants_cuda
from core.ai.recognizer import FaceRecognizer
from core.ai.engine import AI_Engine


class RuntimeTests(unittest.TestCase):
    def test_catalog_faceid_cpu_override_never_initializes_cuda(self):
        with patch("core.ai.recognizer.ensure_faceid_models_available"), patch.object(FaceRecognizer, "_cuda_runtime_ready") as cuda, patch.object(FaceRecognizer, "_create_app") as create:
            recognizer = FaceRecognizer(use_cuda=False)
            cuda.assert_not_called()
            create.assert_called_once_with((320, 320), False)
            self.assertEqual(recognizer.fallback_reason, "CPU explicitly selected")

    def test_thread_limit_reapplied_after_library_override(self):
        with patch.dict(os.environ, {"VISION_OFFICE_CPU_THREADS": "2"}), patch("torch.get_num_threads", return_value=8), patch("torch.set_num_threads") as setter:
            limit_torch_threads()
            setter.assert_called_once_with(2)

    def test_thread_limit_does_not_reset_on_every_frame(self):
        with patch.dict(os.environ, {"VISION_OFFICE_CPU_THREADS": "2"}), patch("torch.get_num_threads", return_value=2), patch("torch.set_num_threads") as setter:
            limit_torch_threads()
            setter.assert_not_called()

    def test_explicit_cpu_never_queries_cuda(self):
        with patch.dict(os.environ, {"VISION_OFFICE_DEVICE": "cpu"}), patch("torch.cuda.is_available") as available:
            self.assertEqual(torch_device()[0], "cpu")
            available.assert_not_called()

    def test_unknown_mode_is_rejected(self):
        with patch.dict(os.environ, {"VISION_OFFICE_DEVICE": "typo"}):
            with self.assertRaises(ValueError):
                wants_cuda()

    def test_no_cuda_driver_returns_cpu(self):
        with patch.dict(os.environ, {"VISION_OFFICE_DEVICE": "auto"}), patch("torch.cuda.is_available", return_value=False):
            self.assertEqual(torch_device()[0], "cpu")

    def test_kernel_failure_returns_cpu(self):
        with patch.dict(os.environ, {"VISION_OFFICE_DEVICE": "auto"}), patch("torch.cuda.is_available", return_value=True), patch("torch.ones", side_effect=RuntimeError("no kernel image")):
            device, reason = torch_device()
            self.assertEqual(device, "cpu")
            self.assertIn("compute test failed", reason)

    def test_faceid_session_initialization_falls_back(self):
        cpu_app = MagicMock()
        with patch("core.ai.recognizer.ensure_faceid_models_available"), patch.object(FaceRecognizer, "_cuda_runtime_ready", return_value=True), patch.object(FaceRecognizer, "_create_app", side_effect=[RuntimeError("missing cudnn"), cpu_app]):
            recognizer = FaceRecognizer()
            self.assertFalse(recognizer.using_cuda)
            self.assertIs(recognizer.app, cpu_app)

    def test_faceid_checks_recognition_even_without_detected_faces(self):
        recognizer = FaceRecognizer.__new__(FaceRecognizer)
        recognizer.app = MagicMock()
        recognizer.app.get.return_value = []
        recognizer.app.models["recognition"].get_feat.return_value = np.ones((1, 512))
        recognizer.sanity_check()
        recognizer.app.models["recognition"].get_feat.assert_called_once()

    def test_faceid_inference_error_retries_once_on_cpu(self):
        recognizer = FaceRecognizer.__new__(FaceRecognizer)
        recognizer.det_size = (320, 320)
        recognizer.using_cuda = True
        recognizer.app = MagicMock()
        recognizer.app.get.side_effect = RuntimeError("out of memory")
        cpu_app = MagicMock()
        cpu_app.get.return_value = []
        with patch.object(recognizer, "_create_app", return_value=cpu_app):
            self.assertIsNone(recognizer.get_embedding(np.zeros((112, 112, 3))))
            self.assertFalse(recognizer.using_cuda)
            self.assertIn("RuntimeError", recognizer.fallback_reason)
            cpu_app.get.assert_called_once()

    def test_cpu_inference_errors_are_not_hidden(self):
        recognizer = FaceRecognizer.__new__(FaceRecognizer)
        recognizer.using_cuda = False
        recognizer.app = MagicMock()
        recognizer.app.get.side_effect = ValueError("invalid input")
        with self.assertRaises(ValueError):
            recognizer.get_embedding(None)

    def test_yolo_fallback_invalidates_previous_tracker_generation(self):
        engine = AI_Engine.__new__(AI_Engine)
        engine.using_cuda = True
        engine.model = MagicMock()
        engine.inference_options = {"device": 0, "imgsz": 960}
        engine.tracker_generation = 0
        engine.track_observations = {1: 4}
        engine.last_retry_at = {1: 2}
        engine.shared_memory = {1: {"name": "Previous identity"}}
        engine.results_lock = threading.Lock()
        engine.last_processed_data = [{"id": 1}]
        with patch("core.ai.engine.detector_model_path", return_value="test-model.pt"), patch("core.ai.engine.YOLO"), patch("torch.cuda.empty_cache"):
            engine._fallback_to_cpu(RuntimeError("OOM"))
        self.assertFalse(engine.using_cuda)
        self.assertEqual(engine.tracker_generation, 1)
        self.assertEqual(engine.inference_options, {"device": "cpu", "imgsz": 960})
        self.assertEqual(engine.shared_memory, {})
        self.assertEqual(engine.last_processed_data, [])


if __name__ == "__main__":
    unittest.main()
