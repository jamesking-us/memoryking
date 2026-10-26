#!/usr/bin/env python3
"""Ensure a wheel contains the same schema as the canonical source."""

import subprocess
import sys
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
wheel = sorted((root / "dist").glob("memoryking-*.whl"))[-1]
with zipfile.ZipFile(wheel) as archive:
    assert (
        archive.read("memoryking/contracts/schema.json")
        == (root / "contracts/foundation-v1.schema.json").read_bytes()
    )
    assert "memoryking/contracts/generated.py" in archive.namelist()
    assert "memoryking/contracts/experience_generated.py" in archive.namelist()
    for stem in ("viewer", "reporting"):
        assert f"memoryking/contracts/{stem}_generated.py" in archive.namelist()
        assert archive.read(f"memoryking/contracts/{stem}.schema.json") == (
            root / f"contracts/{stem}-v1.schema.json"
        ).read_bytes()
    assert (
        archive.read("memoryking/contracts/experience.schema.json")
        == (root / "contracts/experience-v1.schema.json").read_bytes()
    )
    assert "memoryking/reference_agent.py" in archive.namelist()
    entrypoints = [
        name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
    ]
    assert len(entrypoints) == 1
    for entry, module in (
        ("memoryking", "__main__"),
        ("memoryking-migrate", "migration"),
        ("memoryking-embedding-worker", "embedding_worker"),
        ("memoryking-shared", "shared"),
    ):
        assert f"{entry} = memoryking.{module}:main" in archive.read(entrypoints[0]).decode()
        assert f"memoryking/{module}.py" in archive.namelist()
    for resource in (Path("src/memoryking/adapters")).rglob("*"):
        if resource.is_file() and resource.suffix in {".sql", ".json"}:
            assert archive.read(str(resource.relative_to("src"))) == resource.read_bytes()
    assert (
        "memoryking-reference-agent = memoryking.reference_agent:main"
        in archive.read(entrypoints[0]).decode()
    )
    assert (
        "memoryking-repository-worker = memoryking.repository_worker:main"
        in archive.read(entrypoints[0]).decode()
    )
    assert "memoryking/repository_worker.py" in archive.namelist()
    for resource in (
        "local_schema.sql",
        "local_wire.schema.json",
        "knowledge_schema.sql",
        "knowledge.schema.json",
        "retrieval_schema.sql",
        "working_schema.sql",
        "handoff_schema.sql",
        "repository_schema.sql",
        "repository.schema.json",
        "optional_schema.sql",
        "experience_schema.sql",
        "experience.schema.json",
        "inspection.schema.json",
        "postgres_experience_schema.sql",
        "handoff.schema.json",
        "inference.schema.json",
        "working.schema.json",
        "stdio.tools.json",
        "retrieval.schema.json",
        "tokenizers/cl100k_base.tiktoken",
        "tokenizers/LICENSE",
        "web/index.html",
        "web/app.js",
        "web/app.css",
        "web/elephant.png",
        "web/crown.svg",
        *[f"web/{name}.svg" for name in ("favicon", "wordmark", "crown-mono")],
        *[
            f"web/{name}.js"
            for name in ("observatory", "graph", "graph-layout", "graph-worker", "brand")
        ],
    ):
        assert (
            archive.read("memoryking/adapters/" + resource)
            == (root / "src/memoryking/adapters" / resource).read_bytes()
        )
entry = Path(sys.executable).with_name("memoryking-reference-agent")
result = subprocess.run([str(entry), "--help"], capture_output=True, text=True, timeout=10)
assert result.returncode == 0 and "create-file" in result.stdout
worker = Path(sys.executable).with_name("memoryking-repository-worker")
result = subprocess.run([str(worker), "--help"], capture_output=True, text=True, timeout=10)
assert result.returncode == 0 and "--once" in result.stdout
print("Packaged schema and Python types verified:", wheel.name)
