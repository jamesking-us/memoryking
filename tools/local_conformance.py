"""Product scenario adapter using private disposable SQLite stores, never fixture answers."""

import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from knowledge_demo import body as claim_body

from memoryking.adapters.knowledge_store import Knowledge
from memoryking.adapters.local_store import RETRY_MS, LocalStore
from memoryking.domain.local import LocalError, SystemClock


class ScenarioClock(SystemClock):
    def __init__(self):
        self.offset = 0

    def utc_ms(self):
        return super().utc_ms() + self.offset

    def monotonic_ms(self):
        return super().monotonic_ms() + self.offset


class SQLiteScenarios:
    name = "sqlite-local-v1"
    supported_scenarios = frozenset(
        {
            "S01",
            "S02",
            "S05",
            "S06",
            "S07",
            "S13",
            "S14",
            "S15",
            "S16",
            "S20",
            "S21",
            "S22",
            "S25",
            "S27",
        }
    )

    def __init__(self):
        self.temp = None
        self.store = None

    def close(self):
        if self.store:
            self.store.close()
            self.store = None
        if self.temp:
            self.temp.cleanup()
            self.temp = None

    def reset(self, seed):
        self.close()
        self.temp = tempfile.TemporaryDirectory(prefix="mk-scenario-", dir="/tmp")
        self.base = Path(self.temp.name)
        self.root, self.control = self.base / "db", self.base / "ctl"
        self.project = seed["project_id"]
        self.credential = LocalStore.initialize(
            self.root, self.control, seed["tenant_id"], seed["principal_id"]
        ).read_text()
        self.clock = ScenarioClock()
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        self.namespace = self.store.namespace(self.credential, self.project)

    def body(self, key, text):
        return {
            "version": 1,
            "project": self.project,
            "source": "scenario",
            "source_event": key,
            "text": text,
            "readers": None,
            "classification": "internal",
            "observed_at_ms": 0,
        }

    def execute(self, operation, arguments):
        if operation in {"compile_current_context", "brief_after_code_change"}:
            from repository_conformance import exercise

            return exercise(
                self.store, self.credential, self.project, self.base, operation, arguments
            )
        if operation in {"reset_and_resume", "crash_and_resume_harness"}:
            from harness_demo import exercise_harness

            return exercise_harness(
                self.store,
                self.control / "owner.credential",
                self.base,
                project=self.project,
                constraint=arguments.get("constraint", "Never retry an unknown payment outcome"),
                failed=arguments.get("failed_attempt", "blind retry"),
            )
        if operation == "materialize_handoff":
            from handoff_demo import exercise_handoff

            return exercise_handoff(
                self.store,
                self.control / "owner.credential",
                self.project,
                arguments["sender"],
                arguments["recipient"],
                arguments["verification_gap"],
            )
        if operation in {
            "compile_irrelevant_context",
            "compile_mandatory_overflow",
            "resolve_mandatory_constraints",
            "repeat_cached_query_after_changes",
            "replay_then_rerun",
        }:
            from retrieval_scenarios import execute

            return execute(self, operation, arguments)
        if operation in {"supersede_then_query_history", "capture_and_recall_episode"}:
            text = arguments.get("failure", "Observed timeout correction")
            captured = self.store.capture(
                self.credential, self.namespace, "episode", self.body("episode", text)
            )
            span = {
                "observation": captured["observation"],
                "start_byte": 0,
                "end_byte": len(text.encode()),
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            }
            knowledge = Knowledge(self.store)

            def command(action, args, key=None):
                value = {
                    "version": 1,
                    "incarnation": self.store.incarnation,
                    "action": action,
                    "args": {"project": self.project, **args},
                }
                if key:
                    value["key"] = key
                emitted = []
                knowledge.execute(self.credential, value).emit(emitted.append)
                return json.loads(emitted[0])

            first = command(
                "propose",
                {
                    "logical_key": "test",
                    "expected_version": 0,
                    "body": claim_body(span, "original"),
                },
                "proposal1",
            )
            if operation == "capture_and_recall_episode":
                out = []
                self.store.read(self.credential, self.project, query=text).emit(out.append)
                items = json.loads(out[0])["items"]
                proposed = command("query", {"bindings": [{}]})
                return {
                    "attributed_episode_recalled": any(
                        i["text"] == text
                        and i["authority"] == "observed_episode"
                        and i["principal"]
                        for i in items
                    ),
                    "generalization_promoted": bool(proposed["items"]),
                }
            command(
                "review",
                {
                    "revision": first["revision"],
                    "expected_version": 1,
                    "decision": "accept",
                    "reason": "reviewed",
                    "breadth_reviewed": True,
                },
                "accept1",
            )
            recorded = self.store.status(self.credential, self.project)["position"]
            second = command(
                "propose",
                {
                    "logical_key": "test",
                    "expected_version": 2,
                    "body": claim_body(span, "successor"),
                },
                "proposal2",
            )
            command(
                "review",
                {
                    "revision": second["revision"],
                    "expected_version": 3,
                    "decision": "accept",
                    "reason": "corrected",
                    "breadth_reviewed": True,
                    "replacement": {
                        "predecessor": first["revision"],
                        "selector": claim_body(span, "")["selector"],
                        "valid_start": None,
                        "valid_end": None,
                    },
                },
                "accept2",
            )
            historical = command(
                "query",
                {
                    "bindings": [{}],
                    "valid_ms": arguments["as_of_ms"],
                    "recorded_position": recorded,
                },
            )
            current = command("query", {"bindings": [{}], "valid_ms": arguments["as_of_ms"]})
            correct = (
                historical["items"][0]["revision"] == first["revision"]
                and current["items"][0]["revision"] == second["revision"]
            )
            return {"revision": arguments["original"] if correct else "incorrect_history"}
        if operation == "restore_then_read":
            deleted = None
            for position in range(1, int(arguments["backup_frontier"]) + 1):
                deleted = self.store.capture(
                    self.credential,
                    self.namespace,
                    f"k{position}",
                    self.body(f"e{position}", arguments["deleted_id"]),
                )
            backup = self.base / "backup"
            self.store.backup(backup)
            while (
                self.store.status(self.credential, self.project)["position"]
                < int(arguments["deletion_frontier"]) - 1
            ):
                self.store.capture(
                    self.credential, self.namespace, "later", self.body("later", "later")
                )
            self.store.purge(self.credential, deleted["observation"])
            before = self.store.anchor["journal_sequence"]
            self.store.close()
            self.store = None
            LocalStore.restore(self.root, self.control, backup)
            self.credential = (self.control / "owner.credential").read_text()
            self.store = LocalStore(self.root, self.control)
            packets = []
            cursor = None
            while True:
                emitted = []
                self.store.read(self.credential, self.project, cursor=cursor).emit(emitted.append)
                page = json.loads(emitted[0])
                packets.extend(page["items"])
                cursor = page["cursor"]
                if cursor is None:
                    break
            return {
                "journal_replayed_before_reads": int(
                    self.store.db.execute("SELECT value FROM meta WHERE key='journal'").fetchone()[
                        0
                    ]
                )
                >= before,
                "deleted_visible": any(item["id"] == deleted["observation"] for item in packets),
            }
        if operation != "race_idempotent_mutations":
            raise ValueError("unsupported operation")
        if set(arguments["faults"]) != {"crash_after_commit", "expired_result", "purged_result"}:
            raise ValueError("unsupported fault")
        key = arguments["key"]
        reserved, release = threading.Event(), threading.Event()

        def pause(point):
            if point == "after_blob":
                reserved.set()
                if not release.wait(5):
                    raise RuntimeError("race timeout")

        self.store.fault = pause

        def capture(text):
            try:
                return self.store.capture(
                    self.credential, self.namespace, key, self.body(key, text)
                )
            except LocalError as exc:
                return exc.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(capture, arguments["payloads"][0])
            if not reserved.wait(5):
                raise RuntimeError("reservation timeout")
            rest = [pool.submit(capture, text) for text in arguments["payloads"][1:]]
            release.set()
            results = [first.result(), *(future.result() for future in rest)]
        self.store.fault = lambda _: None
        equal = results[0] == results[1]
        different = results[2] == "idempotency_conflict"
        mutations = self.store.db.execute("SELECT count(*) FROM observations").fetchone()[0]
        self.store.close()
        self.store = None
        # Terminate a separate process after a real retry transaction commits, then retry.
        script = """
import json,os,sys
from pathlib import Path
from memoryking.adapters.local_store import LocalStore
root,ctl=Path(sys.argv[1]),Path(sys.argv[2])
def crash(point):
    if point=='after_commit': os._exit(73)
with LocalStore(root,ctl,fault=crash) as store:
    store.capture((ctl/'owner.credential').read_text(),sys.argv[3],sys.argv[4],json.loads(sys.argv[5]))
"""
        crash = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(self.root),
                str(self.control),
                self.namespace,
                key,
                json.dumps(self.body(key, arguments["payloads"][0])),
            ],
            capture_output=True,
            env={k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}},
        )
        if crash.returncode != 73:
            raise RuntimeError("crash did not run")
        self.store = LocalStore(self.root, self.control, clock=self.clock)
        equal = equal and capture(arguments["payloads"][0]) == results[0]
        self.clock.offset += RETRY_MS + 1000
        self.store.expire_results()
        expired = capture(arguments["payloads"][0]) == "idempotency_result_expired"
        self.store.purge(self.credential, results[0]["observation"])
        purged = capture(arguments["payloads"][0]) == "idempotency_result_expired"
        return {
            "mutations": mutations if equal else -1,
            "different_conflicts": different,
            "expired_key_reusable": not (expired and purged),
        }
