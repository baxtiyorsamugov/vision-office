import py_compile
import unittest
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DockerAssetTests(unittest.TestCase):
    def test_compose_has_isolated_runtime_services(self):
        compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        services = compose["services"]
        self.assertEqual(set(services), {"migrate", "vision-worker", "api", "ui"})
        self.assertEqual(services["vision-worker"]["environment"]["VISION_OFFICE_HEADLESS"], "true")
        self.assertEqual(services["ui"]["environment"]["VISION_OFFICE_MANAGED_RUNTIME"], "true")
        self.assertEqual(services["migrate"]["restart"], "no")

    def test_docker_healthcheck_is_valid_python(self):
        py_compile.compile(str(PROJECT_ROOT / "docker" / "healthcheck.py"), doraise=True)

    def test_docker_image_uses_cpu_torch_and_headless_runtime(self):
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        requirements = (PROJECT_ROOT / "requirements-docker-cpu.txt").read_text(encoding="utf-8")
        self.assertIn("torch==2.5.1+cpu", dockerfile)
        self.assertIn("opencv-python-headless", requirements)


if __name__ == "__main__":
    unittest.main()
