import ast
import re
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

    def test_store_sql_inserts_name_their_columns(self):
        """A positional INSERT breaks silently when a column is added; every insert lists its columns."""
        root = Path(__file__).resolve().parents[1] / "src/memoryking"
        positional = re.compile(
            r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+[\w.{}]+\s*(?:VALUES|SELECT)\b", re.IGNORECASE
        )
        found = []
        for path in sorted(root.rglob("*")):
            if path.suffix not in (".py", ".sql"):
                continue
            for match in positional.finditer(path.read_text()):
                found.append(f"{path.relative_to(root)}: {match.group(0)}")
        self.assertEqual(found, [], "name the columns: INSERT INTO table(col,...) VALUES (...)")
