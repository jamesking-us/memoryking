"""The PostgreSQL facade binds placeholders and refuses engine-specific syntax; not runtime evidence."""

import unittest

from memoryking.adapters.postgres_sql import Record, native_statement
from memoryking.application.storage import (
    PostgresDialect,
    SQLiteDialect,
    insert_ignore,
    upsert,
)
from memoryking.domain.local import LocalError


class StatementTests(unittest.TestCase):
    def test_parameter_quoting_and_refusal_of_engine_specific_syntax(self):
        self.assertEqual(
            native_statement("SELECT '?' AS value WHERE x=? AND y='it''s ?'"),
            "SELECT '?' AS value WHERE x=%s AND y='it''s ?'",
        )
        # Portable statements pass through untouched apart from placeholders.
        self.assertEqual(
            native_statement("SELECT blob,blob_hash FROM observations WHERE tenant=?"),
            "SELECT blob,blob_hash FROM observations WHERE tenant=%s",
        )
        self.assertEqual(
            native_statement(upsert("grants")),
            "INSERT INTO grants (tenant,principal,project,capabilities,starts,expires) VALUES "
            "(%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant,principal,project) DO UPDATE SET "
            "capabilities=EXCLUDED.capabilities,starts=EXCLUDED.starts,expires=EXCLUDED.expires",
        )
        self.assertTrue(native_statement(insert_ignore("conflicts", 4)).endswith("DO NOTHING"))
        self.assertIn("websearch_to_tsquery", PostgresDialect().lexical_match())
        self.assertEqual(PostgresDialect().byte_length("body"), "octet_length(body)")
        self.assertEqual(SQLiteDialect().byte_length("body"), "length(CAST(body AS BLOB))")
        # Engine-specific syntax is refused rather than translated.
        for sql in [
            "PRAGMA user_version",
            "BEGIN IMMEDIATE",
            "INSERT OR REPLACE INTO grants VALUES (?,?,?,?,?,?)",
            "INSERT OR IGNORE INTO conflicts VALUES (?,?,?,?)",
            "SELECT id FROM lexical WHERE lexical MATCH ?",
            "SELECT length(CAST(body AS BLOB)) FROM checkpoints",
            "SELECT 1 WHERE name GLOB ?",
        ]:
            with self.assertRaises(LocalError):
                native_statement(sql)

    def test_row_named_and_positional_access(self):
        row = Record(("tenant", "position"), ("a", 3))
        self.assertEqual(row["tenant"], "a")
        self.assertEqual(row[1], 3)
        self.assertEqual(list(row), ["a", 3])
        self.assertEqual(dict(row), {"tenant": "a", "position": 3})
