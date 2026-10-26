"""Local Chromium UI coverage over disposable canonical stores, never real accounts/mail."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import platform
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def seed(fixture):
    from test_report_catalog import CatalogCases
    from test_report_document import phase_source
    from test_report_store import ReportCases
    from test_working import checkpoint

    from memoryking.adapters.working import Working

    fixture.store.namespace(fixture.owner, "q")
    fixture.grant_viewer(version=2, project="q")
    marker = "Synthetic finding: a timeout must cover each retry attempt."
    raw = (
        marker
        + "\n<script>window.PWNED=1</script> ![tracker](https://tracker.invalid/pixel.png) javascript:alert(1)\nhttps://example.invalid/evidence"
    )
    obs = fixture.store.capture(
        fixture.owner,
        fixture.ns,
        "mobile-evidence",
        {
            "version": 1,
            "project": "p",
            "source": "synthetic-regression-run",
            "source_event": "mobile-evidence",
            "text": raw,
            "readers": ["owner", "phone"],
            "classification": "internal",
            "observed_at_ms": 1793000000000,
        },
    )
    body = checkpoint()
    body.update(
        objective="Fix checkout retry handling",
        current_state="Retry logic updated. Regression evidence captured; device validation remains pending.",
        references=[],
        verification=[],
    )
    body["repository"]["artifacts"] = []
    body["acceptance"] = [
        {
            "id": "regression",
            "text": "Retry regression recorded",
            "state": "passed",
            "evidence": [{"kind": "observation", "id": obs["observation"]}],
            "decision": None,
        },
        {
            "id": "device",
            "text": "Validate on Android",
            "state": "pending",
            "evidence": [],
            "decision": None,
        },
        {
            "id": "browser",
            "text": "Legacy browser support deferred",
            "state": "waived",
            "evidence": [],
            "decision": "synthetic_scope_decision",
        },
        {
            "id": "timeout",
            "text": "Deadline stress check",
            "state": "failed",
            "evidence": [],
            "decision": None,
        },
        {
            "id": "changed",
            "text": "Original requirement",
            "state": "passed",
            "evidence": [{"kind": "observation", "id": obs["observation"]}],
            "decision": None,
        },
    ]
    for revision in (1, 2):
        if revision == 2:
            body["acceptance"][-1]["text"] = "Changed requirement needs new proof"
        Working(fixture.store).execute(
            fixture.owner,
            {
                "version": 1,
                "incarnation": fixture.store.incarnation,
                "action": "save",
                "key": f"mobile-task-{revision}",
                "args": {
                    "project": "p",
                    "task": "timeout",
                    "run": None,
                    "expected_revision": revision - 1,
                    "readers": ["owner", "phone"],
                    "checkpoint": body,
                },
            },
        )

    class Catalog(CatalogCases):
        def command(self, action, args, credential=None, key=None):
            if action == "submit":
                args = {**args, "task": "timeout"}
            return ReportCases.command(self, action, args, credential, key)

    catalog = Catalog()
    catalog.store, catalog.owner, catalog.n = fixture.store, fixture.owner, 10000
    catalog.actor = fixture.actor_store
    code = "print('🦣: preserve exact source')\n" * 180
    extra = (
        "\n\n### ADR: Current access before display\n\nNo private offline cache.\n\n```python\n"
        + code
        + "```\n\n| State | Meaning |\n| --- | --- |\n| unknown | New proof required |\n"
    )
    extra += "\n\n" + "\n\n".join(
        f"Evidence paragraph {i}: complete report content." for i in range(85)
    )
    with patch("test_report_catalog.phase_source", lambda suffix: phase_source(extra + suffix)):
        files = catalog.publish("mobile-report", ["owner", "phone"], image=True)
    from memoryking.reporting.template import model

    return {
        "marker": marker,
        "code": code.rstrip("\n"),
        "model": model(files["source.md"].decode(), "Synthetic phase"),
    }


def run(fixture, transport, args, backend):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    from memoryking.domain.local import LocalError

    expected = seed(fixture)
    origin = fixture.settings["public_origin"]
    cert = x509.load_pem_x509_certificate((fixture.root / "viewer.crt").read_bytes())
    spki = base64.b64encode(
        hashlib.sha256(
            cert.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ).digest()
    ).decode()
    exchange = fixture.root / "controlled-exchange.json"

    class Provider:
        def authenticate(self, code, verifier, callback, deadline):
            query = json.loads(exchange.read_text())
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            if (
                code != "controlled-code"
                or challenge != query["code_challenge"]
                or callback != origin + "/viewer/auth/github/callback"
            ):
                raise LocalError("login_failed")
            return "12345"

    fixture.auth.provider = Provider()
    command_file, acknowledgement = (
        fixture.root / "operator-command.json",
        fixture.root / "operator-ack.json",
    )
    stopped = threading.Event()
    control_errors = []

    def operator():
        while not stopped.wait(0.025):
            if not command_file.exists():
                continue
            try:
                command = json.loads(command_file.read_text())
                if command != {"action": "disable", "id": 1}:
                    raise ValueError("Unexpected fixture operator command")
                version = fixture.store.db.execute(
                    "SELECT version FROM viewer_bindings WHERE tenant=? AND principal=?",
                    (fixture.tenant, "phone"),
                ).fetchone()[0]
                fixture.policy("disable", {"principal": "phone", "expected_version": version})
                acknowledgement.write_text(json.dumps({"id": 1, "completed": True}))
            except Exception as error:
                control_errors.append(type(error).__name__)
                acknowledgement.write_text(json.dumps({"id": 1, "completed": False}))
            return

    controller = threading.Thread(target=operator)
    inputs = fixture.root / "mobile-browser.json"
    output_dir = args.output.parent / f"{backend}-{transport}"
    output_dir.mkdir(parents=True, exist_ok=True)
    inputs.write_text(
        json.dumps(
            {
                "chrome": str(args.chrome),
                "playwright": str(args.playwright),
                "origin": origin,
                "spki": spki,
                "certificate": (fixture.root / "viewer.crt").read_text(),
                "exchange": str(exchange),
                "operator_command": str(command_file),
                "operator_ack": str(acknowledgement),
                "output": str(output_dir.resolve()),
                **expected,
            }
        )
    )
    controller.start()
    try:
        subprocess.run(
            [str(args.node), str(ROOT / "tools/mobile-viewer-browser-probe.mjs"), str(inputs)],
            check=True,
            timeout=180,
        )
    finally:
        stopped.set()
        controller.join(timeout=6)
    assert not controller.is_alive() and not control_errors, control_errors
    assert json.loads(acknowledgement.read_text())["completed"]
    return {
        "backend": backend,
        "transport": transport,
        **json.loads((output_dir / "result.json").read_text()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "chrome", "playwright", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--quick", action="store_true", help="SQLite/direct-TLS only; not the full matrix"
    )
    args = parser.parse_args()
    import test_viewer_auth_http as http
    from viewer_auth_browser_probe import gateway

    backends = (
        (http.LocalViewerAuthHTTP,)
        if args.quick
        else (http.LocalViewerAuthHTTP, http.PostgresViewerAuthHTTP)
    )
    results = []
    if not args.quick:
        http.PostgresViewerAuthHTTP.setUpClass()
    try:
        for backend in backends:
            for transport in ("direct_tls",) if args.quick else ("direct_tls", "isolated_proxy"):
                fixture = backend("test_actual_login_cookie_flags_private_read_and_logout")
                fixture.setUp()
                try:
                    if transport == "direct_tls":
                        with fixture.listener(tls=True):
                            results.append(run(fixture, transport, args, backend.__name__))
                    else:
                        with gateway(fixture) as origin, fixture.listener(public_origin=origin):
                            results.append(run(fixture, transport, args, backend.__name__))
                finally:
                    fixture.tearDown()
    finally:
        if not args.quick:
            http.PostgresViewerAuthHTTP.tearDownClass()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "passed": all(r["passed"] for r in results),
                "quick": args.quick,
                "platform": platform.platform(),
                "scope": "Real local desktop Chromium and canonical HTTP/store reads with controlled GitHub; synthetic corpus and lifecycle/clock injection. Not physical Android, real GitHub/Tailscale, install or TalkBack qualification.",
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Mobile viewer browser: {len(results)} store/transport combinations passed")


if __name__ == "__main__":
    main()
