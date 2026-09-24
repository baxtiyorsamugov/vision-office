import py_compile
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DockerAssetTests(unittest.TestCase):
    def test_compose_has_isolated_runtime_services(self):
        compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        services = compose["services"]
        self.assertEqual(set(services), {"postgres", "migrate", "vision-worker", "edge-sync", "unknown-clusterer", "api", "ui"})
        self.assertEqual(services["postgres"]["image"], "postgres:16-alpine")
        self.assertEqual(services["migrate"]["depends_on"]["postgres"]["condition"], "service_healthy")
        self.assertEqual(services["vision-worker"]["environment"]["VISION_OFFICE_HEADLESS"], "true")
        self.assertEqual(services["vision-worker"]["environment"]["VISION_OFFICE_EXTERNAL_EDGE_SYNC"], "true")
        self.assertEqual(services["ui"]["environment"]["VISION_OFFICE_MANAGED_RUNTIME"], "true")
        self.assertEqual(services["migrate"]["restart"], "no")
        for service_name in {"migrate", "vision-worker", "edge-sync", "unknown-clusterer", "api", "ui"}:
            self.assertEqual(
                services[service_name]["environment"]["YOLO_CONFIG_DIR"],
                "/tmp/vision-office-yolo",
            )
        self.assertEqual(services["unknown-clusterer"]["cpus"], "0.50")
        self.assertEqual(services["unknown-clusterer"]["healthcheck"]["timeout"], "20s")

    def test_docker_healthcheck_is_valid_python(self):
        with tempfile.TemporaryDirectory() as directory:
            py_compile.compile(str(PROJECT_ROOT / "docker" / "healthcheck.py"),
                               cfile=str(Path(directory) / "healthcheck.pyc"), doraise=True)

    def test_worker_health_requires_fresh_ai_ready_status(self):
        from docker import healthcheck
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "data").mkdir()
            (root / "config/settings.yaml").write_text("cameras:\n  - id: test\n", encoding="utf-8")
            status = root / "data/runtime_status_test.json"
            with patch.object(healthcheck, "PROJECT_ROOT", root):
                status.write_text(json.dumps({"camera_id": "test", "running": True}))
                self.assertFalse(healthcheck._worker_ok())
                status.write_text(json.dumps({"camera_id": "test", "running": True, "ai_ready": True}))
                self.assertTrue(healthcheck._worker_ok())
                stale = time.time() - 120
                os.utime(status, (stale, stale))
                self.assertFalse(healthcheck._worker_ok())

    def test_gpu_overlay_changes_only_camera_service(self):
        overlay = yaml.safe_load((PROJECT_ROOT / "docker-compose.gpu.yml").read_text(encoding="utf-8"))
        self.assertEqual(set(overlay["services"]), {"vision-worker"})
        worker = overlay["services"]["vision-worker"]
        self.assertEqual(worker["build"]["target"], "gpu")
        self.assertEqual(worker["deploy"]["resources"]["reservations"]["devices"][0]["capabilities"], ["gpu"])
        compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        for name, service in compose["services"].items():
            if name != "postgres":
                self.assertEqual(service["environment"]["VISION_OFFICE_DEVICE"], "cpu")

    def test_transfer_excludes_device_specific_runtime(self):
        from tools.create_update_archive import should_skip
        self.assertTrue(should_skip(PROJECT_ROOT / "docker-compose.override.yml"))
        self.assertFalse(should_skip(PROJECT_ROOT / "docker-compose.gpu.yml"))
        self.assertFalse(should_skip(PROJECT_ROOT / "start_vision_office.ps1"))

    def test_docker_image_uses_cpu_torch_and_headless_runtime(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        requirements = (PROJECT_ROOT / "requirements-docker-cpu.txt").read_text(encoding="utf-8")
        self.assertIn("torch==2.5.1+cpu", dockerfile)
        self.assertIn("opencv-python-headless", requirements)
        self.assertIn("ENTRYPOINT [\"./docker/entrypoint.sh\"]", dockerfile)


if __name__ == "__main__":
    unittest.main()
