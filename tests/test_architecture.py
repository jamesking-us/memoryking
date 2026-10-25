import ast
import unittest
from pathlib import Path


class ArchitectureTests(unittest.TestCase):
    def test_domain_does_not_import_adapters_or_vendor_libraries(self):
        root = Path(__file__).resolve().parents[1]
        forbidden = ("memoryking.adapters", "sqlite3", "psycopg", "httpx", "fastapi", "openai")
        for path in (root / "src/memoryking/domain").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                if isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                self.assertFalse(any(name.startswith(forbidden) for name in names), path)
