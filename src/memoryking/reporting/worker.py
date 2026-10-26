"""Fixed sandboxed renderer entry point. No store, service token or SMTP imports."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import signal
import sys
from pathlib import Path

from memoryking.contracts.canonical import canonical_bytes, load_strict
from memoryking.domain.local import LocalError
from memoryking.domain.reporting import ARTIFACT_LIMIT, BUILD_TIMEOUT, SOURCE_LIMIT
from memoryking.reporting.template import model, reader_pages


def build(stage: Path) -> None:
    os.umask(0o077)
    # Also bound an orphan after the supervising service is killed. The durable
    # worker lease outlives this alarm before another renderer may be started.
    signal.alarm(BUILD_TIMEOUT)
    # OS limits supplement the parent wall-clock/RSS monitor and input/image bounds.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (ARTIFACT_LIMIT, ARTIFACT_LIMIT))
    resource.setrlimit(resource.RLIMIT_CPU, (BUILD_TIMEOUT, BUILD_TIMEOUT))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    source_path = stage / "source.md"
    if source_path.stat().st_size > SOURCE_LIMIT:
        raise LocalError("report_source_limit")
    metadata = load_strict((stage / "request.json").read_bytes())
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"title"}
        or not isinstance(metadata["title"], str)
    ):
        raise LocalError("invalid_report_request")
    source = source_path.read_text(encoding="utf-8")
    document = model(source, metadata["title"])
    # Optional dependencies are loaded only in the restricted worker.
    from memoryking.reporting.formats import render_epub, render_pdf, validate_epub

    render_pdf(
        source_path, stage / "report.pdf", title=metadata["title"], subtitle="Full phase report"
    )
    render_epub(source_path, stage / "report.epub", title=metadata["title"])
    validate_epub(stage / "report.epub")
    from pypdf import PdfReader

    pdf_pages = len(PdfReader(stage / "report.pdf").pages)
    (stage / "reader.json").write_bytes(canonical_bytes(document))
    pages = reader_pages(document)
    (stage / "reader-pages.json").write_bytes(canonical_bytes([json.loads(p) for p in pages]))
    artifacts = []
    artifact_bytes = 0
    for name in ["report.pdf", "report.epub", "reader.json", "reader-pages.json"]:
        data = (stage / name).read_bytes()
        artifact_bytes += len(data)
        artifacts.append(
            {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        )
    if artifact_bytes > ARTIFACT_LIMIT:
        raise LocalError("report_artifact_limit")
    (stage / "result.json").write_bytes(
        canonical_bytes(
            {
                "version": 1,
                "artifacts": artifacts,
                "reader_pages": len(pages),
                "pdf_pages": pdf_pages,
            }
        )
    )


if __name__ == "__main__":
    build(Path(sys.argv[1]))
