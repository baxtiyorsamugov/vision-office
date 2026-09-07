import tempfile
import time
import unittest
import os
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from core.config import ConfigurationError, load_app_settings
from core.edge.config import EdgeSettings
from core.events import RecognitionEventStore
import core.logging_setup as logging_setup
from core.health import HealthChecker, HealthSettings
from core.hr import HRManager
from core.ai.engine import _load_known_faces
from core import performance
from core.preview import CameraPreviewPublisher, preview_path
from core.video.streamer import safe_source_label
from database.models import Attendance, Employee, HealthIncident
from database.manager import database_url
from database.migrations import run_migrations
from main import headless_mode


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "office.db"
        self.engine = create_engine(f"sqlite:///{self.db_path}")

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def test_migrations_create_event_and_health_tables(self):
        run_migrations(self.engine)
        tables = set(inspect(self.engine).get_table_names())
        self.assertTrue({"recognition_events", "health_incidents", "notification_outbox", "remote_person_reference_photos", "schema_migrations"}.issubset(tables))

    def test_migrations_upgrade_legacy_remote_person_table(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE remote_persons (id VARCHAR(36) PRIMARY KEY, person_type VARCHAR(20) NOT NULL, fio VARCHAR(255), embedding JSON, active BOOLEAN, updated_at DATETIME NOT NULL)"
            )
        run_migrations(self.engine)
        columns = {item["name"] for item in inspect(self.engine).get_columns("remote_persons")}
        self.assertTrue({"photo_url", "photo_path", "embedding_status", "embedding_error"}.issubset(columns))

    def test_event_store_deduplicates_same_subject_per_camera(self):
        run_migrations(self.engine)
        store = RecognitionEventStore(self.engine, cooldown_seconds=60)
        identity = {"person_id": "person-1", "person_type": "employee", "name": "Test Person"}
        first = store.record(
            camera_id="entry", event_type="entry", identity=identity,
            confidence=0.8, embedding=np.ones(512, dtype=np.float32), image=None,
        )
        second = store.record(
            camera_id="entry", event_type="entry", identity=identity,
            confidence=0.8, embedding=np.ones(512, dtype=np.float32), image=None,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_local_employee_is_merged_into_the_erp_matching_cache(self):
        run_migrations(self.engine)
        session = sessionmaker(bind=self.engine)()
        try:
            employee = Employee(full_name="Local Person", role="Сотрудник", face_embeddings=[[0.2] * 512])
            session.add(employee)
            session.commit()
            session.refresh(employee)
        finally:
            session.close()

        class FakeEdgeService:
            @staticmethod
            def cache_embeddings():
                return ([{"name": "ERP Person", "person_id": "erp-person", "person_type": "employee"}], np.ones((1, 512), dtype=np.float32))

        identities, embeddings = _load_known_faces(sessionmaker(bind=self.engine), FakeEdgeService(), remote_mode=True)
        self.assertEqual(embeddings.shape, (2, 512))
        self.assertEqual(identities[0]["person_id"], "erp-person")
        self.assertEqual(identities[1]["person_id"], f"local:{employee.id}")
        self.assertEqual(identities[1]["person_type"], "local_employee")

    def test_local_attendance_is_stored_while_erp_identity_is_ignored(self):
        run_migrations(self.engine)
        session = sessionmaker(bind=self.engine)()
        try:
            employee = Employee(full_name="Local Person", role="Сотрудник", face_embeddings=[[0.2] * 512])
            session.add(employee)
            session.commit()
            session.refresh(employee)
        finally:
            session.close()
        hr = HRManager(cooldown_minutes=0, engine=self.engine)
        hr.register_presence({"person_id": f"local:{employee.id}", "person_type": "local_employee", "name": employee.full_name}, event_type="exit")
        hr.register_presence({"person_id": "erp-person", "person_type": "employee", "name": "ERP Person"}, event_type="entry")
        session = sessionmaker(bind=self.engine)()
        try:
            records = session.query(Attendance).all()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].employee_id, employee.id)
            self.assertEqual(records[0].event_type, "exit")
        finally:
            session.close()

    def test_preview_publisher_writes_bounded_local_jpeg(self):
        output_directory = Path(self.tempdir.name) / "previews"
        publisher = CameraPreviewPublisher(
            "entry / main",
            max_fps=8,
            max_width=320,
            jpeg_quality=70,
            output_directory=output_directory,
        ).start()
        try:
            started = time.monotonic()
            publisher.submit(np.zeros((240, 640, 3), dtype=np.uint8))
            self.assertLess(time.monotonic() - started, 0.1)
            target = preview_path("entry / main", output_directory)
            for _ in range(30):
                if target.is_file():
                    break
                time.sleep(0.02)
            self.assertTrue(target.is_file())
            image = cv2.imread(str(target))
            self.assertEqual(image.shape[1], 320)
            self.assertTrue(publisher.stats()["preview_available"])
        finally:
            publisher.stop()

    def test_config_rejects_duplicate_camera_ids(self):
        settings = Path(self.tempdir.name) / "settings.yaml"
        settings.write_text(
            "database:\n  path: data/test.db\ncameras:\n  - id: same\n    rtsp_url: rtsp://one\n  - id: same\n    rtsp_url: rtsp://two\n",
            encoding="utf-8",
        )
        with self.assertRaises(ConfigurationError):
            load_app_settings(settings)

    def test_edge_configuration_rejects_placeholder_credentials(self):
        settings = EdgeSettings(
            enabled=True,
            base_url="https://erp.example.test",
            device_api_key="REPLACE_ON_DEVICE_ONLY",
            device_id="device-1",
        )
        self.assertIn("device_api_key", settings.validation_error())

    def test_edge_defaults_to_hourly_people_sync(self):
        self.assertEqual(EdgeSettings().sync_interval_seconds, 3600)

    def test_database_url_uses_postgresql_config_when_requested(self):
        settings = Path(self.tempdir.name) / "settings.yaml"
        settings.write_text(
            "database:\n  type: postgresql\n  host: postgres\n  port: 5432\n  name: vision\n  user: edge\n  password: local password\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"VISION_OFFICE_DATABASE_URL": ""}, clear=False):
            self.assertEqual(
                database_url(settings),
                "postgresql+psycopg://edge:local+password@postgres:5432/vision",
            )

    def test_rotating_log_file_is_created(self):
        original_dir = logging_setup.LOG_DIR
        logger = __import__("logging").getLogger("vision_office")
        old_handlers = list(logger.handlers)
        for handler in old_handlers:
            logger.removeHandler(handler)
            handler.close()
        try:
            logging_setup.LOG_DIR = Path(self.tempdir.name) / "logs"
            configured = logging_setup.configure_logging("INFO")
            configured.info("logging smoke test")
            for handler in configured.handlers:
                handler.flush()
            self.assertTrue((logging_setup.LOG_DIR / "vision-office.log").is_file())
        finally:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            logger.handlers = old_handlers
            logging_setup.LOG_DIR = original_dir

    def test_health_checker_records_and_recovers_incident(self):
        run_migrations(self.engine)
        checker = HealthChecker(
            HealthSettings(enabled=True, failure_threshold=1),
            EdgeSettings(),
            engine=self.engine,
        )
        checker._database_status = lambda: ("ok", "database ready")
        checker._internet_status = lambda: ("ok", "internet ready")
        checker._disk_status = lambda: ("ok", "disk ready")
        checker._edge_status = lambda: ("ok", "ERP disabled")
        checker._camera_statuses = lambda: {"camera:entry": ("failed", "stream lost")}
        checker.check_once()
        session = checker.Session()
        try:
            incident = session.query(HealthIncident).one()
            self.assertEqual(incident.status, "open")
        finally:
            session.close()
        checker._camera_statuses = lambda: {"camera:entry": ("ok", "stream restored")}
        checker.check_once()
        session = checker.Session()
        try:
            incident = session.query(HealthIncident).one()
            self.assertEqual(incident.status, "recovered")
            self.assertIsNotNone(incident.recovered_at)
        finally:
            session.close()

    def test_health_checker_flags_stale_configured_camera_and_ignores_test_artifact(self):
        checker = HealthChecker(HealthSettings(camera_stale_seconds=15), EdgeSettings(), engine=self.engine)
        checker._configured_camera_ids = lambda: {"entry"}
        with patch("core.health.read_runtime_status", return_value={
            "cameras": [
                {"camera_id": "entry", "running": True, "stream_status": "connected", "frame_age_ms": 16000},
                {"camera_id": "test_video", "running": False, "stream_status": "reconnecting", "frame_age_ms": 999999},
            ]
        }):
            statuses = checker._camera_statuses()
        self.assertEqual(set(statuses), {"camera:entry"})
        self.assertEqual(statuses["camera:entry"][0], "failed")

    def test_status_file_lock_does_not_raise_or_stop_camera(self):
        destination = Path(self.tempdir.name) / "runtime.json"
        with patch("core.performance.os.replace", side_effect=PermissionError("file locked")):
            self.assertFalse(performance._write_status_file(destination, {"running": True}))

    def test_headless_mode_is_opt_in(self):
        previous = os.environ.get("VISION_OFFICE_HEADLESS")
        try:
            os.environ["VISION_OFFICE_HEADLESS"] = "true"
            self.assertTrue(headless_mode())
            os.environ["VISION_OFFICE_HEADLESS"] = "0"
            self.assertFalse(headless_mode())
        finally:
            if previous is None:
                os.environ.pop("VISION_OFFICE_HEADLESS", None)
            else:
                os.environ["VISION_OFFICE_HEADLESS"] = previous

    def test_camera_log_label_redacts_rtsp_credentials(self):
        label = safe_source_label("rtsp://admin:secret-password@192.168.0.2:554/Streaming/Channels/101")
        self.assertEqual(label, "rtsp://192.168.0.2:554/Streaming/Channels/101")
        self.assertNotIn("secret-password", label)


if __name__ == "__main__":
    unittest.main()
