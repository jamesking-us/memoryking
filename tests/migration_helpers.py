"""Construct historical SQLite fixture inventories, including later-phase removals."""

import re
from pathlib import Path


def historical_inventory(db, version):
    root = Path(__file__).resolve().parents[1] / "src/memoryking/adapters"
    migrations = {
        2: "knowledge",
        3: "retrieval",
        4: "working",
        5: "handoff",
        6: "repository",
        7: "optional",
        8: "experience",
        9: "viewer",
    }
    for number, name in reversed(list(migrations.items())):
        if number > version:
            source = (root / f"{name}_schema.sql").read_text()
            for table in reversed(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", source)):
                db.execute("DROP TABLE IF EXISTS " + table)
    db.execute(f"PRAGMA user_version={version}")
