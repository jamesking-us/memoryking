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
        9: "index",
        10: "egress",
        11: "summary",
        12: "trust",
        13: "anchor",
    }
    for name in ("report", "viewer"):
        source = (root / f"{name}_schema.sql").read_text()
        for table in reversed(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", source)):
            db.execute("DROP TABLE IF EXISTS " + table)
    db.execute("DELETE FROM meta WHERE key='mobile_schema'")
    for number, name in reversed(list(migrations.items())):
        if number > version:
            source = (root / f"{name}_schema.sql").read_text()
            for table in reversed(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", source)):
                db.execute("DROP TABLE IF EXISTS " + table)
    db.execute(f"PRAGMA user_version={version}")


def historical_private_inventory(db, version):
    """Construct the actual private 9/10 lineage, distinct from native 9/10."""
    if version not in (9, 10):
        raise ValueError("private predecessor must be 9 or 10")
    historical_inventory(db, 8)
    root = Path(__file__).resolve().parents[1] / "src/memoryking/adapters"
    db.executescript((root / "viewer_schema.sql").read_text())
    if version == 10:
        db.executescript((root / "report_schema.sql").read_text())
