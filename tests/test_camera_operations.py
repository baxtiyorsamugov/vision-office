import json
import os
import queue
import tempfile
import threading
import time
import unittest
import uuid
from collections import deque
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.analytics import daily_analytics
from core.camera_controls import read_controls, save_camera
from core.config import CameraSettings, ConfigurationError, load_app_settings
from core.performance import _read_fresh_status, configured_camera_statuses
from core.supervisor import CameraSupervisor
from core.ai.engine import AI_Engine, face_recognition_worker, SEARCHING_STATUS
from core.ai.cache import FaceCache
from core.ai.recognizer import FaceRecognizer
from core.video.streamer import VideoStream
from database.models import Base, RecognitionEvent


class CameraControlsTests(unittest.TestCase):
    def test_paused_cameras_are_not_a_health_failure(self):
        from core.health import HealthChecker
        checker = HealthChecker.__new__(HealthChecker)
        with patch.object(checker, "_configured_camera_ids", return_value=set()), patch("core.health.read_runtime_status", return_value={}):
            self.assertEqual(checker._camera_statuses()["application"][0], "ok")

    def test_controls_preserve_base_yaml_and_detect_conflicting_editor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "controls.json"
            config = root / "settings.yaml"
            original = "database:\n  path: data/test.db\ncameras:\n  - id: entry\n    rtsp_url: rtsp://test\neduschool_turnstile:\n  enabled: true\n"
            config.write_text(original)
            camera = CameraSettings("exit", "rtsp://user:secret@camera/2", name="Exit", event_type="exit")
            save_camera(camera, expected_revision="", path=path)
            settings = load_app_settings(config, controls_path=path)
            self.assertEqual([item.id for item in settings.cameras], ["entry", "exit"])
            self.assertEqual(config.read_text(), original)
            with self.assertRaises(ConfigurationError):
                save_camera(replace(camera, is_active=False), expected_revision="", path=path)
            save_camera(replace(camera, is_active=False), expected_revision=read_controls(path)[1], path=path)
            self.assertFalse(load_app_settings(config, controls_path=path).cameras[1].is_active)

    def test_invalid_camera_never_overwrites_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controls.json"
            for camera in (CameraSettings("../bad", "rtsp://host"), CameraSettings("a", "rtsp://"),
                           CameraSettings("a", "rtsp://host:bad"), CameraSettings("a", "rtsp://host", event_type="wrong")):
                with self.assertRaises(ConfigurationError):
                    save_camera(camera, expected_revision="", path=path)
            self.assertFalse(path.exists())

    def test_stale_status_cannot_claim_camera_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps({"running": True, "ai_ready": True, "stream_status": "connected"}))
            os.utime(path, (time.time() - 60, time.time() - 60))
            status = _read_fresh_status(path)
            self.assertFalse(status["running"])
            self.assertEqual(status["stream_status"], "stale")

    def test_unconfigured_runtime_files_do_not_appear_in_dashboard(self):
        settings = SimpleNamespace(cameras=(CameraSettings("entry", "rtsp://host"),))
        with patch("core.performance.read_runtime_status", return_value={"cameras": [{"camera_id": "test_video", "running": True}]}):
            result = configured_camera_statuses(settings)
        self.assertEqual([row["camera_id"] for row in result], ["entry"])
        self.assertFalse(result[0]["running"])

    def test_supervisor_only_restarts_changed_camera(self):
        first = CameraSettings("entry", "rtsp://host")
        second = CameraSettings("exit", "rtsp://host/2")
        supervisor = CameraSupervisor((first, second), {}, "INFO")
        supervisor.context = MagicMock()
        supervisor.reconcile((first, second), {})
        self.assertEqual(supervisor.context.Process.call_count, 2)
        supervisor.context.Process.reset_mock()
        with patch("core.supervisor.write_camera_runtime_status"):
            supervisor.reconcile((replace(first, profile="economy"), second), {})
        self.assertEqual(supervisor.context.Process.call_count, 1)
        self.assertEqual(supervisor.context.Process.call_args.kwargs["args"][0]["id"], "entry")
        with patch("core.supervisor.write_camera_runtime_status"):
            supervisor.reconcile((replace(first, is_active=False), replace(second, is_active=False)), {})
        self.assertEqual(supervisor.processes, {})


class AnalyticsTests(unittest.TestCase):
    def make_engine(self):
        return create_engine("sqlite://")

    def test_eduschool_and_same_name_people_are_counted_by_id_with_tashkent_boundary(self):
        engine = self.make_engine()
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            for index, (person, kind, hour, direction) in enumerate([
                ("a", "eduschool_employee", 19, "entry"), ("a", "eduschool_employee", 20, "exit"),
                ("b", "eduschool_employee", 20, "entry"), ("c", "eduschool_student", 20, "entry"),
                ("d", "local_employee", 20, "entry"), ("z", "unknown", 20, "entry"),
                ("old", "eduschool_employee", 18, "entry"),
            ]):
                session.add(RecognitionEvent(id=str(index), person_id=person, person_type=kind,
                    person_name="Same Name", camera_id="c", event_type=direction, subject_signature=person,
                    created_at=datetime(2026, 10, 7, hour, tzinfo=timezone.utc)))
            session.commit()
        result = daily_analytics(engine, date(2026, 10, 8), page_size=2, page=99)
        self.assertEqual((result["total"], result["entries"], result["exits"]), (4, 4, 1))
        self.assertEqual((result["page"], len(result["rows"])), (2, 2))
        self.assertEqual(result["hourly"][0]["entries"], 1)
        students = daily_analytics(engine, date(2026, 10, 8), person_type="eduschool_student")
        self.assertEqual(students["total"], 1)
        self.assertEqual(daily_analytics(engine, date(2026, 10, 8), search="absent")["total"], 0)
        engine.dispose()


@unittest.skipUnless(os.environ.get("CATALOG_TEST_DATABASE_URL"), "isolated PostgreSQL URL not supplied")
class PostgreSQLAnalyticsTests(AnalyticsTests):
    def make_engine(self):
        url = os.environ["CATALOG_TEST_DATABASE_URL"]
        admin = create_engine(url)
        schema = "analytics_test_" + uuid.uuid4().hex
        with admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema} -ctimezone=Asia/Tashkent"})
        def cleanup():
            engine.dispose()
            with admin.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            admin.dispose()
        self.addCleanup(cleanup)
        return engine


class InferenceSchedulingTests(unittest.TestCase):
    def test_worker_drops_expired_tasks_revalidates_without_duplicate_event_and_recovers(self):
        from core.edge.config import EdgeSettings
        tasks = queue.Queue()
        image = np.zeros((100, 80, 3), dtype=np.uint8)
        tasks.put((1, image, time.monotonic() - 10))
        tasks.put((2, image, time.monotonic()))
        tasks.put((3, image, time.monotonic()))
        tasks.put((3, image, time.monotonic()))
        tasks.put((4, image, time.monotonic()))
        tasks.put((5, image, time.monotonic()))
        tasks.put(None)
        shared = {key: {"name": "Analyzing..."} for key in (1, 3, 4, 5)}
        metrics = {}
        identity = {"name": "Local Test", "person_id": "local:1", "person_type": "local_employee"}
        vector = np.ones(512, dtype=np.float32) / np.sqrt(512)
        orthogonal = vector.copy()
        orthogonal[:256] *= -1
        weak_match = 0.3 * vector + np.sqrt(1 - 0.3**2) * orthogonal
        engine = create_engine("sqlite://")
        with patch("core.ai.recognizer.FaceRecognizer") as recognizer, \
             patch("core.edge.config.load_edge_settings", return_value=EdgeSettings()), \
             patch("core.eduschool.catalog.load_settings", return_value=SimpleNamespace(recognition_threshold=0.55)), \
             patch("core.unknown_visitors.load_unknown_visitor_settings", return_value=SimpleNamespace(enabled=False)), \
             patch("database.manager.get_engine", return_value=engine), \
             patch("core.events.RecognitionEventStore") as events, \
             patch("core.ai.engine._load_known_faces", return_value=([identity], np.array([vector]))):
            recognizer.return_value.using_cuda = False
            recognizer.return_value.fallback_reason = None
            recognizer.return_value.get_embedding.side_effect = [vector, vector, vector, RuntimeError("test failure"), weak_match]
            face_recognition_worker(tasks, shared, [], metrics)
        self.assertEqual(events.return_value.record.call_count, 2)
        self.assertIsNone(events.return_value.record.call_args_list[1].kwargs["identity"])
        self.assertEqual(metrics["expired"], 2)
        self.assertEqual(metrics["errors"], 1)
        self.assertEqual(shared[4]["name"], SEARCHING_STATUS)
        self.assertEqual(shared[3]["person_id"], "local:1")
        engine.dispose()

    def test_cache_refresh_keeps_last_good_snapshot_and_expires_on_failure(self):
        loader = MagicMock(side_effect=[(["first"], None), RuntimeError("offline"), (["second"], None)])
        cache = FaceCache(loader)
        cache.refresh()
        self.assertEqual(cache.snapshot[0], ["first"])
        self.assertEqual(cache.error, "RuntimeError")
        cache.last_success -= 31
        self.assertTrue(cache.stale)
        cache.refresh()
        self.assertFalse(cache.stale)
        self.assertEqual(cache.snapshot[0], ["second"])
        self.assertIsNone(cache.error)

    def test_disconnected_reader_stops_without_waiting_for_backoff(self):
        stream = VideoStream("rtsp://host/test")
        with patch.object(stream, "_open", return_value=False):
            stream.start()
            started = time.monotonic()
            stream.stop()
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(stream.thread.is_alive())

    def make_engine(self):
        engine = AI_Engine.__new__(AI_Engine)
        engine.model = MagicMock()
        boxes = MagicMock()
        boxes.xyxy.cpu.return_value.numpy.return_value = np.array([[20*i, 0, 20*i+60, 100] for i in range(5)])
        boxes.id.cpu.return_value.numpy.return_value = np.arange(5)
        engine.model.track.return_value = [SimpleNamespace(boxes=boxes)]
        engine.track_observations = {i: 2 for i in range(5)}
        engine.track_seen_at = {99: time.monotonic() - 20}
        engine.last_retry_at = {}
        engine.shared_memory = {99: {"name": "Old"}}
        engine.tracker_generation = 0
        engine.inference_options = {}
        engine.using_cuda = False
        engine.detection_timings = deque(maxlen=100)
        engine.detection_started_at = deque(maxlen=100)
        engine.face_metrics = {}
        engine.results_lock = threading.Lock()
        engine.inference_lock = threading.Lock()
        engine.input_queue = queue.Queue(maxsize=2)
        return engine

    def test_all_faces_display_and_waiting_tracks_get_next_turn_without_stretch(self):
        engine = self.make_engine()
        frame = np.zeros((150, 200, 3), dtype=np.uint8)
        with patch("core.ai.engine.limit_torch_threads"):
            engine._run_detection(frame)
            first = [engine.input_queue.get_nowait() for _ in range(2)]
            self.assertEqual(len(engine.last_processed_data), 5)
            self.assertNotIn(99, engine.shared_memory)
            self.assertNotEqual(first[0][1].shape[0], first[0][1].shape[1])
            for track_id, _, _ in first:
                engine.shared_memory[track_id] = {"name": "Known", "person_id": str(track_id)}
            engine._run_detection(frame)
            # Queue-full tracks use the existing retry interval before their next turn.
            engine.last_retry_at = {key: value - 1 for key, value in engine.last_retry_at.items()}
            engine._run_detection(frame)
            later = [engine.input_queue.get_nowait() for _ in range(engine.input_queue.qsize())]
        self.assertTrue(any(task[0] >= 2 for task in later))

    def test_faceid_selects_central_face_instead_of_larger_neighbour(self):
        recognizer = FaceRecognizer.__new__(FaceRecognizer)
        recognizer.using_cuda = False
        recognizer.app = MagicMock()
        neighbour = SimpleNamespace(bbox=np.array([0, 0, 30, 100]), embedding=np.zeros(512))
        target = SimpleNamespace(bbox=np.array([35, 20, 70, 80]), embedding=np.ones(512))
        recognizer.app.get.return_value = [neighbour, target]
        result = recognizer.get_embedding(np.zeros((100, 100, 3)), target_center=True)
        np.testing.assert_array_equal(result, target.embedding)

    def test_usb_zero_is_numeric_and_rtsp_open_is_bounded(self):
        with patch("core.video.streamer.cv2.VideoCapture") as capture:
            capture.return_value.read.return_value = (True, np.zeros((20, 20, 3)))
            VideoStream("0")._open()
            self.assertEqual(capture.call_args.args, (0,))
            VideoStream("rtsp://host/test")._open()
            self.assertIn(3000, capture.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
