import tempfile
import unittest
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, inspect

from core.config import ConfigurationError, load_app_settings
from core.edge.config import EdgeSettings
from core.events import RecognitionEventStore
import core.logging_setup as logging_setup
from core.health import HealthChecker, HealthSettings
from database.models import HealthIncident
from database.migrations import run_migrations


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
        self.assertTrue({"recognition_events", "health_incidents", "notification_outbox", "schema_migrations"}.issubset(tables))

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


if __name__ == "__main__":
    unittest.main()
