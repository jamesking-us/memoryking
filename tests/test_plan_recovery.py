"""Actual backup/restore preserves plans while retiring their old write/read namespace."""

import json
import tempfile
import unittest
from pathlib import Path

import psycopg
import test_postgres_store as pg
from test_plans import plan

from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.working import Working
from memoryking.domain.local import LocalError


def execute(store, credential, action, args, key=None, incarnation=None):
    request = {
        "version": 1,
        "incarnation": incarnation or store.incarnation,
        "action": action,
        "args": {"project": "demo", **args},
    }
    if key is not None:
        request["key"] = key
    result = []
    Working(store).execute(credential, request).emit(result.append)
    return json.loads(result[0])


def seed(store, credential):
    for name in ("a", "b"):
        execute(
            store,
            credential,
            "plan_save",
            {"task": name, "expected_revision": 0, "readers": ["owner"], "plan": plan()},
            f"save-{name}",
        )
    cursor = execute(store, credential, "plan_list", {"limit": 1, "cursor": None})["cursor"]
    return store.incarnation, cursor


def reconcile(store, credential, history, cursor):
    with unittest.TestCase().assertRaises(LocalError):
        execute(store, credential, "plan_list", {"limit": 1, "cursor": cursor})
    with unittest.TestCase().assertRaisesRegex(LocalError, "history_changed"):
        execute(store, credential, "plan_get", {"task": "a", "revision": None}, incarnation=history)
    with unittest.TestCase().assertRaisesRegex(LocalError, "history_changed"):
        execute(
            store,
            credential,
            "plan_save",
            {"task": "a", "expected_revision": 0, "readers": ["owner"], "plan": plan()},
            "save-a",
            incarnation=history,
        )
    with unittest.TestCase().assertRaisesRegex(LocalError, "checkpoint_reconciliation_required"):
        execute(
            store,
            credential,
            "plan_save",
            {"task": "a", "expected_revision": 1, "readers": ["owner"], "plan": plan()},
            "new-save",
        )
    execute(
        store,
        credential,
        "assign_writer",
        {
            "task": "a",
            "run": None,
            "writer": "owner",
            "expected_revision": 1,
            "reason": "restore_reconciliation",
        },
        "reconcile",
    )
    execute(
        store,
        credential,
        "plan_save",
        {"task": "a", "expected_revision": 2, "readers": ["owner"], "plan": plan()},
        "new-save",
    )
    value = execute(store, credential, "plan_get", {"task": "a", "revision": None})
    assert value["revision"] == 3 and value["progress"]["completed"] == 0


class LocalPlanRecovery(unittest.TestCase):
    def test_actual_restore_and_explicit_plan_reconciliation(self):
        with tempfile.TemporaryDirectory(prefix="mk-plan-restore-", dir="/tmp") as folder:
            root = Path(folder)
            data = root / "data"
            control = root / "control"
            archive = root / "archive"
            credential = LocalStore.initialize(data, control).read_text()
            with LocalStore(data, control) as store:
                history, cursor = seed(store, credential)
                store.backup(archive)
            credential = LocalStore.restore(data, control, archive).read_text()
            with LocalStore(data, control) as store:
                reconcile(store, credential, history, cursor)


class PostgresPlanRecovery(unittest.TestCase):
    setUpClass = classmethod(pg.PostgresStoreTests.setUpClass.__func__)
    tearDownClass = classmethod(pg.PostgresStoreTests.tearDownClass.__func__)
    setUp = pg.PostgresStoreTests.setUp
    store = pg.PostgresStoreTests.store

    def test_actual_native_restore_and_explicit_plan_reconciliation(self):
        from memoryking.adapters.postgres_recovery import backup, restore

        archive = self.base / (self.tenant + "-plan-backup")
        with self.store() as store:
            history, cursor = seed(store, self.credential)
        with (
            self.store() as store,
            psycopg.connect(self.cluster.dsn("mk_backup"), autocommit=True) as connection,
        ):
            backup(store, connection, archive)
        with psycopg.connect(self.cluster.dsn("mk_migration"), autocommit=True) as connection:
            self.credential = restore(
                connection, self.root, self.control, self.tenant, archive, clock=self.clock
            ).read_text()
        with self.store() as store:
            reconcile(store, self.credential, history, cursor)
