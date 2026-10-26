"""Execute explicit mobile product probes. Pending phases never count as passing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

METHODS = {
    "AC01": ["test_ac01_numeric_binding_rename_duplicate_and_denial"],
    "AC02": [
        "test_ac02_project_grant_never_shares_owner_only_content",
        "test_ac02_viewer_cannot_select_or_write_another_tenant",
        "test_ac02_task_projection_counts_only_visible_checkpoints",
    ],
    "AC03": [
        "test_ac03_retries_ceiling_logout_and_binding_changes",
        "test_ac03_concurrent_expected_version_only_one_wins",
        "test_ac03_after_journal_crash_replays_one_policy_result",
        "test_ac03_disable_and_content_grant_cannot_be_bypassed",
        "test_owner_wire_closed_contracts_and_no_mcp_discovery",
    ],
    "AC04": ["test_ac04_real_restore_preserves_current_revoke_and_history_fences"],
    "AC05": ["test_ac05_persisted_evaluation_instant_and_release_deadline"],
}


class EvidenceResult(unittest.TextTestResult):
    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.probes[test.id()] = {
            "status": "passed",
            "seconds": round(time.monotonic() - self.started, 4),
        }

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.probes[test.id()] = {"status": "failed"}

    def addError(self, test, err):
        super().addError(test, err)
        self.probes[test.id()] = {"status": "failed"}

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.probes[test.id()] = {"status": "not_evaluated", "reason": reason}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["M01"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (Path(os.environ.get("MEMORYKING_PG_BIN", "")) / "pg_ctl").is_file():
        raise SystemExit(
            "Both-store mobile conformance requires the explicit native PostgreSQL runtime"
        )
    expected = {
        case: [
            f"test_viewer_identity.{backend}.{method}"
            for backend in ("LocalViewerIdentity", "PostgresViewerIdentity")
            for method in methods
        ]
        for case, methods in METHODS.items()
    }
    extras = [
        "test_viewer_migrations.LocalViewerMigrations.test_schema_eight_upgrade_rolls_back_then_retries",
        "test_viewer_migrations.LocalViewerMigrations.test_viewer_control_corruption_blocks_startup",
        "test_viewer_migrations.NativeViewerStorage.test_viewer_tables_are_tenant_isolated_and_worker_read_only",
        "test_viewer_migrations.NativeViewerUpgrade.test_schema_three_upgrade_is_atomic_and_repeatable",
        "test_viewer_contracts.ViewerContractTests.test_closed_config_origin_tls_and_bounds",
        "test_viewer_contracts.ViewerContractTests.test_subjects_keys_and_readonly_dtos_are_closed",
    ]
    probes = sorted({name for names in expected.values() for name in names} | set(extras))
    suite = unittest.defaultTestLoader.loadTestsFromNames(probes)
    with __import__("tempfile").TemporaryFile(mode="w+") as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=EvidenceResult)
        # Per-run storage, not shared class state between executions.
        EvidenceResult.probes = {}
        outcome = result.run(suite)
        log.seek(0)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.with_suffix(".log").write_text(log.read())
    evidence = outcome.probes
    complete = (
        outcome.wasSuccessful()
        and len(evidence) == len(probes)
        and all(evidence.get(name, {}).get("status") == "passed" for name in probes)
    )
    cases = {
        case: {
            "status": "passed"
            if all(evidence.get(name, {}).get("status") == "passed" for name in names)
            else "not_passed",
            "probes": names,
        }
        for case, names in expected.items()
    }
    modules = ("test_viewer_identity", "test_viewer_migrations", "test_viewer_contracts")
    args.output.write_text(
        json.dumps(
            {
                "program": "mobile-v1",
                "phase": args.phase,
                "passed": complete,
                "scope": "Actual foundation layer; no GitHub provider, viewer HTTP, PWA or device qualification",
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cases": cases,
                "probes": evidence,
                "test_sources": {
                    name: hashlib.sha256((ROOT / "tests" / f"{name}.py").read_bytes()).hexdigest()
                    for name in modules
                },
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Mobile M01: {len(evidence)}/{len(probes)} probes observed; passed={complete}")
    raise SystemExit(0 if complete else 1)


if __name__ == "__main__":
    main()
