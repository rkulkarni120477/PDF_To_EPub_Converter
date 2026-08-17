import ast
from pathlib import Path
import unittest


SERVICES = [
    "api-gateway",
    "pdf-extraction-service",
    "content-processing-service",
    "content-enrichment-service",
    "metadata-service",
    "media-asset-service",
    "preview-service",
    "epub-composition-service",
    "validation-service",
    "version-service",
    "collaboration-service",
    "packaging-service",
    "job-orchestration-service",
    "notification-service",
    "download-service",
    "reporting-service",
]


class ServiceStructureTests(unittest.TestCase):
    def test_each_service_has_runnable_boundary(self):
        root = Path(__file__).parents[1]
        for service in SERVICES:
            service_root = root / "services" / service
            main_path = service_root / "app" / "main.py"
            self.assertTrue(main_path.exists(), service)
            self.assertTrue((service_root / "requirements.txt").exists(), service)
            self.assertTrue((service_root / "Dockerfile").exists(), service)
            tree = ast.parse(main_path.read_text(encoding="utf-8"))
            routes = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
            self.assertTrue(any(node.name == "health" for node in routes), service)
            self.assertTrue(any(node.name != "health" for node in routes), service)


if __name__ == "__main__":
    unittest.main()
