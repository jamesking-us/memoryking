"""Statement-adapter checks are not PostgreSQL runtime conformance evidence."""

import unittest

from memoryking.adapters.postgres_sql import Record, native_statement
from memoryking.domain.local import LocalError


class StatementTests(unittest.TestCase):
    def test_parameter_quoting_and_native_operations(self):
        self.assertEqual(
            native_statement("SELECT '?' AS value WHERE x=? AND y='it''s ?'"),
            "SELECT '?' AS value WHERE x=%s AND y='it''s ?'",
        )
        self.assertIn(
            "phraseto_tsquery", native_statement("SELECT id FROM lexical WHERE lexical MATCH ?")
        )
        self.assertEqual(
            native_statement("SELECT length(CAST(body AS BLOB)) FROM checkpoints"),
            "SELECT octet_length(body) FROM checkpoints",
        )
        self.assertIn(
            "ON CONFLICT (tenant,principal,project) DO UPDATE SET",
            native_statement("INSERT OR REPLACE INTO grants VALUES (?,?,?,?,?,?)"),
        )
        self.assertTrue(
            native_statement("INSERT OR IGNORE INTO conflicts VALUES (?,?,?,?)").endswith(
                "ON CONFLICT DO NOTHING"
            )
        )
        for sql in [
            "PRAGMA user_version",
            "BEGIN IMMEDIATE",
            "INSERT OR REPLACE INTO unknown VALUES (?)",
        ]:
            with self.assertRaises(LocalError):
                native_statement(sql)

    def test_row_named_and_positional_access(self):
        row = Record(("tenant", "position"), ("a", 3))
        self.assertEqual(row["tenant"], "a")
        self.assertEqual(row[1], 3)
        self.assertEqual(list(row), ["a", 3])
        self.assertEqual(dict(row), {"tenant": "a", "position": 3})
