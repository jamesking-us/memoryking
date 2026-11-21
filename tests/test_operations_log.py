"""The operations log, its compaction, and the instance lease, on both profiles.

`OperationsLogTests` run against the local store here and, adopted unchanged, against
PostgreSQL in test_postgres_operations_log.py. `OperationsLogFileTests` exercise the log
files directly.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_knowledge as knowledge_helpers
import test_working as working_helpers

from memoryking.adapters import operations_log
from memoryking.adapters.local_files import Control
from memoryking.adapters.local_store import LocalStore
from memoryking.adapters.operations_log import SEGMENT_LIMIT, oldest_coverage
from memoryking.domain.local import LocalError


def code_of(call):
    try:
        call()
    except LocalError as exc:
        return exc.code
    return None


class OperationsLogTests(unittest.TestCase):
    """Store-level behavior; `build`, `archive` and `restore_from` adapt to the backend."""

    setUp = knowledge_helpers.KnowledgeTests.setUp
    tearDown = knowledge_helpers.KnowledgeTests.tearDown
    fails = working_helpers.WorkingTests.fails
    clock = working_helpers.WorkingTests.clock
    scope = working_helpers.WorkingTests.scope
    command = working_helpers.WorkingTests.command
    execute = working_helpers.WorkingTests.execute
    save = working_helpers.WorkingTests.save
    intent = working_helpers.WorkingTests.intent
    transition = working_helpers.WorkingTests.transition
    operations = working_helpers.WorkingTests.operations

    # -- backend adapters ---------------------------------------------------------------------

    def build(self, **kwargs):
        return LocalStore(self.data, self.control, **kwargs)

    def archive(self, destination):
        self.store.backup(destination)

    def restore_from(self, archive):
        self.store.close()
        self.owner = LocalStore.restore(
            self.data, self.control, archive, clock=self.store.clock
        ).read_text()
        self.store = self.build(clock=self.store.clock)

    def reopen(self, **kwargs):
        clock = self.store.clock
        self.store.close()
        self.store = self.build(clock=clock, **kwargs)

    # -- helpers -------------------------------------------------------------------------------

    def tree(self):
        return Path(self.store.control_path)

    def log(self):
        return self.store.control.operations

    def life(self, operation):
        """One operation from intent to a reconciled result: three log records."""
        self.intent(operation)
        self.transition("dispatching", 1, operation=operation)
        self.transition("succeeded", 2, operation=operation)

    def segments(self):
        return sorted(p.name for p in (self.tree() / "operations").glob("*.log"))

    def position(self, key):
        return self.store.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]

    # -- the log ---------------------------------------------------------------------------------

    def test_operation_records_go_to_their_own_log_and_leave_the_journal_alone(self):
        self.save()
        journal = (self.tree() / "journal").read_bytes()
        count = self.store.anchor["journal_sequence"]
        self.life("op1")
        self.assertEqual((self.tree() / "journal").read_bytes(), journal)
        self.assertEqual(self.store.anchor["journal_sequence"], count)
        self.assertEqual(self.store.anchor["operations_sequence"], 3)
        self.assertEqual(self.position("operations"), "3")
        self.assertEqual(self.position("journal"), str(count))
        self.assertEqual(self.segments(), ["00000001.log"])
        # A new process verifies the same log against the anchor.
        self.reopen()
        self.assertEqual(self.operations()[0]["state"], "succeeded")

    def test_records_chain_across_rolled_segments_and_each_sealed_one_has_a_trailer(self):
        self.save()
        with mock.patch.object(operations_log, "SEGMENT_LIMIT", 1):
            self.life("op1")
        self.assertEqual(self.segments(), ["00000001.log", "00000002.log", "00000003.log"])
        first = (self.tree() / "operations" / "00000001.log").read_text().splitlines()
        self.assertEqual(len(first), 2)  # the record and its trailer
        self.assertEqual(
            len((self.tree() / "operations" / "00000003.log").read_text().splitlines()), 1
        )
        self.reopen()
        self.assertEqual(self.store.control.operations.verified_count(self.store.anchor), 3)

    def test_truncating_or_replacing_the_log_fails_closed(self):
        self.save()
        with mock.patch.object(operations_log, "SEGMENT_LIMIT", 1):
            self.life("op1")
        directory = self.tree() / "operations"
        for damage in ("tail", "middle", "content"):
            with self.subTest(damage):
                saved = {p.name: p.read_bytes() for p in directory.glob("*.log")}
                if damage == "tail":
                    (directory / "00000003.log").unlink()
                    expected = "journal_rollback"
                elif damage == "middle":
                    (directory / "00000002.log").unlink()
                    expected = "journal_invalid"
                else:
                    path = directory / "00000001.log"
                    path.write_bytes(path.read_bytes().replace(b"A", b"B", 1))
                    expected = None
                self.store.close()
                try:
                    self.assertTrue(
                        code_of(lambda: self.probe())
                        in ({expected} if expected else {"invalid_token", "journal_invalid"})
                    )
                finally:
                    for name, data in saved.items():
                        (directory / name).write_bytes(data)
                        (directory / name).chmod(0o600)
        self.probe()

    def probe(self):
        """Open the store and run one request; every failure is a LocalError code."""
        self.store = self.build(clock=self.store.clock)
        self.operations()

    def test_operations_are_replayed_on_restore_to_an_older_backup(self):
        self.save()
        self.life("before")
        archive = self.root / "backups" / "a"
        self.archive(archive)
        self.intent("after")
        self.transition("dispatching", 1, operation="after")
        self.restore_from(archive)
        states = {op["id"]: op["state"] for op in self.operations()}
        self.assertEqual(states, {"before": "succeeded", "after": "dispatching"})
        by_id = {op["id"]: op for op in self.operations()}
        self.assertTrue(by_id["after"]["reconciliation_required"])

    def test_records_already_in_the_journal_stay_valid_beside_the_log(self):
        from memoryking.adapters.operations_log import OperationsLog

        self.save()
        archive = self.root / "backups" / "a"
        self.archive(archive)
        legacy = lambda log, anchor, action: log.control.append(anchor, action)  # noqa: E731
        with mock.patch.object(OperationsLog, "append", legacy):
            self.intent("old")  # written to the journal, as stores did before the log
        self.intent("new")
        self.assertEqual(self.store.anchor["operations_sequence"], 1)
        self.restore_from(archive)
        self.assertEqual({op["id"] for op in self.operations()}, {"old", "new"})

    # -- compaction ------------------------------------------------------------------------------

    def compaction_fixture(self):
        """op1 resolved, op2 open, two backups; every record is its own segment."""
        self.save()
        self.backups = self.root / "backups"
        patcher = mock.patch.object(operations_log, "SEGMENT_LIMIT", 1)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.life("op1")  # 1-3
        self.intent("op2")  # 4
        self.archive(self.backups / "a")  # covers 4
        self.life("op3")  # 5-7
        self.archive(self.backups / "b")  # covers 7

    def test_nothing_is_compacted_without_a_backup(self):
        self.compaction_fixture()
        self.assertEqual(self.store.compact_operations(self.root / "none"), 0)
        shutil.rmtree(self.backups)
        self.backups.mkdir()
        self.assertEqual(self.store.compact_operations(self.backups), 0)
        self.assertEqual(self.log().compacted_through(), 0)
        self.assertEqual(len(self.segments()), 7)

    def test_compaction_keeps_what_the_oldest_backup_or_an_open_operation_needs(self):
        self.compaction_fixture()
        # Backup a covers 4, b covers 7; op2 (record 4) is unresolved: only op1's three go.
        self.assertEqual(self.store.compact_operations(self.backups), 3)
        self.assertEqual(self.log().compacted_through(), 3)
        self.assertEqual(self.segments(), [f"{n:08d}.log" for n in range(4, 8)])
        # Resolving op2 lets its intent go, but never what only the newest backup covers.
        self.transition("dispatching", 1, operation="op2")  # 8
        self.transition("succeeded", 2, operation="op2")  # 9
        self.archive(self.backups / "c")  # covers 9
        self.assertEqual(self.store.compact_operations(self.backups), 1)
        self.assertEqual(self.log().compacted_through(), 4)
        shutil.rmtree(self.backups / "a")  # retention removed the oldest
        self.assertEqual(self.store.compact_operations(self.backups), 3)
        self.assertEqual(self.log().compacted_through(), 7)
        self.assertEqual(self.segments(), ["00000008.log", "00000009.log"])
        self.reopen()
        self.assertEqual(self.log().verified_count(self.store.anchor), 9)

    def test_an_unresolved_or_unreconciled_operation_is_never_dropped(self):
        self.save()
        self.backups = self.root / "backups"
        with mock.patch.object(operations_log, "SEGMENT_LIMIT", 1):
            self.intent("open")  # 1, never resolved
            self.life("later")  # 2-4
            self.archive(self.backups / "a")
            self.life("last")  # 5-7
            self.assertEqual(self.store.compact_operations(self.backups), 0)
            self.assertEqual(self.log().compacted_through(), 0)
            # Resolved, but in an earlier history, so not reconciled in this one.
            self.transition("dispatching", 1, operation="open")
            self.transition("succeeded", 2, operation="open")
            self.store.db.execute(
                "UPDATE operation_intents SET incarnation='hist_other' WHERE id='open'"
            )
            self.archive(self.backups / "b")
            self.assertEqual(self.store.compact_operations(self.backups), 0)

    def test_a_compacted_identity_is_consumed_for_ever(self):
        self.compaction_fixture()
        self.store.compact_operations(self.backups)
        self.store.db.execute("DELETE FROM operation_intents WHERE id='op1'")
        self.fails("operation_identity_consumed", lambda: self.intent("op1"))
        self.reopen()
        self.fails("operation_identity_consumed", lambda: self.intent("op1"))
        self.assertTrue(self.log().consumed(self.tenant_of(), "op1"))
        self.assertFalse(self.log().consumed(self.tenant_of(), "op2"))

    def tenant_of(self):
        return self.store.db.execute("SELECT tenant FROM tenants").fetchone()[0]

    def test_a_backup_older_than_the_compaction_cannot_be_restored(self):
        self.save()
        self.life("op1")
        stale = self.root / "outside" / "stale"
        self.archive(stale)  # the store is not told about this one
        self.backups = self.root / "backups"
        with mock.patch.object(operations_log, "SEGMENT_LIMIT", 1):
            self.life("op2")
            self.archive(self.backups / "a")
            self.life("op3")
            self.assertGreater(self.store.compact_operations(self.backups), 0)
        self.fails("backup_invalid", lambda: self.restore_from(stale))
        self.reopen()  # nothing changed
        self.assertEqual(len(self.operations()), 3)
        self.restore_from(self.backups / "a")
        self.assertEqual({op["id"] for op in self.operations()}, {"op1", "op2", "op3"})

    def test_coverage_counts_only_verified_manifests_and_missing_positions_as_zero(self):
        self.save()
        self.life("op1")
        directory = self.root / "backups"
        self.archive(directory / "a")
        self.assertEqual(oldest_coverage(self.store.control, directory), 3)
        (directory / "junk").mkdir()
        (directory / "junk" / "manifest").write_text("not a signed manifest")
        self.assertEqual(oldest_coverage(self.store.control, directory), 3)
        self.assertEqual(oldest_coverage(self.store.control, directory / "absent"), 0)

    # -- the instance lease ----------------------------------------------------------------------

    def lease_row(self):
        row = self.store.db.execute("SELECT holder,renewed_ms FROM instance_lease").fetchone()
        return None if row is None else (row[0], row[1])

    def drop_lease(self):
        self.store.db.execute("DELETE FROM instance_lease")

    def touch(self, store=None):
        store = store or self.store
        with store.transaction():
            pass

    def test_a_restart_with_the_same_holder_is_never_blocked_and_renewal_is_throttled(self):
        clock = self.clock()
        self.drop_lease()
        self.touch()
        holder, renewed = self.lease_row()
        self.assertEqual((holder, renewed), (self.store.lease, clock.utc_ms()))
        clock.advance(14_000)
        self.touch()
        self.assertEqual(self.lease_row()[1], renewed)  # less than 15 s: not written
        clock.advance(1_000)
        self.touch()
        self.assertEqual(self.lease_row()[1], renewed + 15_000)
        clock.advance(10 * 60_000)  # a long outage, then a restart after a crash
        self.reopen()
        self.touch()
        self.assertEqual(self.lease_row()[0], holder)

    def test_another_holder_is_refused_until_the_renewal_is_older_than_60_seconds(self):
        clock = self.clock()
        self.drop_lease()
        self.reopen(instance_id="alpha")
        self.touch()
        self.reopen(instance_id="beta")
        self.assertNotEqual(self.store.lease, self.lease_row()[0])
        self.fails("control_lease_held", self.touch)
        clock.advance(59_999)
        self.fails("control_lease_held", self.touch)
        clock.advance(1)
        self.touch()  # taken over
        self.assertEqual(self.lease_row()[0], self.store.lease)
        self.reopen(instance_id="alpha")
        self.fails("control_lease_held", self.touch)

    def test_an_operator_set_instance_id_is_the_holder_on_any_volume(self):
        self.reopen(instance_id="alpha")
        first = self.store.lease
        self.reopen(instance_id="alpha")
        self.assertEqual(self.store.lease, first)
        self.reopen()
        self.assertNotEqual(self.store.lease, first)
        self.fails("invalid_request", lambda: self.reopen(instance_id="not valid!"))

    def test_event_rows_have_the_on_behalf_of_column_and_it_stays_empty_without_delegation(self):
        self.touch()
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM events WHERE on_behalf_of IS NOT NULL"
            ).fetchone()[0],
            0,
        )

    def test_a_tenant_without_a_lease_row_claims_it_on_its_first_write_and_reads_do_not(self):
        self.drop_lease()
        self.assertIsNone(self.lease_row())
        self.touch()
        self.assertEqual(self.lease_row()[0], self.store.lease)

    def test_restore_resets_the_lease(self):
        clock = self.clock()
        self.save()
        archive = self.root / "backups" / "a"
        self.archive(archive)
        self.drop_lease()
        self.reopen(instance_id="alpha")
        self.touch()
        self.restore_from(archive)
        self.store.close()
        self.store = self.build(clock=clock, instance_id="beta")
        self.touch()  # no wait for alpha's lease to lapse
        self.assertEqual(self.lease_row()[0], self.store.lease)

    def test_a_copied_control_tree_has_a_different_holder_and_is_refused(self):
        self.touch()
        original = self.store.lease
        self.store.close()
        copy = self.root / "copy"
        copy.mkdir(mode=0o700)
        self.copy_to(copy)
        other = self.build_copy(copy)
        try:
            self.assertNotEqual(other.lease, original)
            self.fails("control_lease_held", lambda: self.touch(other))
        finally:
            other.close()
        self.store = self.build(clock=self.store.clock)
        self.touch()  # the original is unaffected

    def copy_to(self, copy):
        shutil.copytree(self.data, copy / "data")
        shutil.copytree(self.control, copy / "control")

    def build_copy(self, copy):
        return LocalStore(copy / "data", copy / "control", clock=self.store.clock)


class OperationsLogFileTests(unittest.TestCase):
    """The log files themselves: chain, trailers, anchor, torn tail, cache and compaction."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-o-")
        base = Path(self.temp.name)
        self.control_dir = base / "control"
        LocalStore.initialize(base / "data", self.control_dir)
        self.control = Control(self.control_dir)
        self.anchor = self.control.read_anchor()
        self.dir = self.control_dir / "operations"
        patcher = mock.patch.object(operations_log, "SEGMENT_LIMIT", 400)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp.cleanup()

    def append(self, count, start=1):
        for number in range(start, start + count):
            self.control.operations.append(
                self.anchor, {"kind": "operation", "tenant": "main", "operation": f"o{number}"}
            )

    def fresh(self):
        return Control(self.control_dir)

    def test_segments_roll_by_size_and_a_new_process_verifies_them(self):
        self.append(12)
        names = sorted(p.name for p in self.dir.glob("*.log"))
        self.assertGreater(len(names), 2)
        fresh = self.fresh()
        self.assertEqual(fresh.operations.verified_count(self.anchor), 12)
        self.assertEqual(
            [r["sequence"] for r in fresh.operations.records_after(self.anchor, 9)], [10, 11, 12]
        )
        self.assertEqual(SEGMENT_LIMIT, 4 * 1024 * 1024)

    def test_the_cache_extends_across_appends_and_rolls_without_reverifying(self):
        self.append(3)
        self.control.operations.sync(self.anchor)
        self.append(9, start=4)
        reader = self.fresh()
        self.assertEqual(reader.operations.verified_count(self.anchor), 12)
        self.append(2, start=13)
        with mock.patch.object(Control, "_verify", side_effect=AssertionError("re-verified")):
            self.assertEqual(self.control.operations.verified_count(self.anchor), 14)

    def test_a_trailer_naming_the_wrong_range_or_head_is_refused(self):
        self.append(6)
        first = self.dir / "00000001.log"
        lines = first.read_text().splitlines()
        self.assertEqual(len(lines), 3)  # two records and a trailer
        trailer = self.control.verify(lines[-1])
        for field, value in (
            ("count", 9),
            ("min_sequence", 5),
            ("previous_segment_head", "1" * 64),
        ):
            with self.subTest(field):
                forged = self.control.sign({**trailer, field: value})
                first.write_text("\n".join([*lines[:-1], forged]) + "\n")
                self.assertEqual(
                    code_of(lambda: self.fresh().operations.verified_count(self.anchor)),
                    "journal_invalid",
                )
        first.write_text("\n".join(lines) + "\n")
        self.assertEqual(self.fresh().operations.verified_count(self.anchor), 6)

    def test_rollback_to_an_earlier_state_fails_closed(self):
        self.append(4)
        saved = {p.name: p.read_bytes() for p in self.dir.glob("*.log")}
        self.append(5, start=5)
        for path in self.dir.glob("*.log"):
            path.unlink()
        for name, data in saved.items():
            (self.dir / name).write_bytes(data)
            (self.dir / name).chmod(0o600)
        self.assertEqual(
            code_of(lambda: self.fresh().operations.verified_count(self.anchor)), "journal_rollback"
        )

    def test_a_torn_final_line_is_discarded_only_after_every_anchored_record(self):
        self.append(2)
        path = self.dir / "00000001.log"
        torn = path.read_bytes() + b"eyJ0b3JuIn0.partial"
        path.write_bytes(torn)
        self.assertEqual(self.fresh().operations.verified_count(self.anchor), 2)
        fresh = self.fresh()
        fresh.operations.append(
            self.anchor, {"kind": "operation", "tenant": "main", "operation": "o3"}
        )
        self.assertEqual(self.fresh().operations.verified_count(self.anchor), 3)
        # The same damage inside the anchored part is fatal.
        data = path.read_bytes()
        path.write_bytes(data[: len(data) - 5])
        self.assertIn(
            code_of(lambda: self.fresh().operations.verified_count(self.anchor)),
            {"journal_invalid", "invalid_token", "journal_rollback"},
        )

    def compact(self, through_sequence):
        return self.control.operations.compact(self.anchor, through_sequence, lambda ids: True)

    def test_compaction_drops_sealed_segments_and_leaves_a_verifiable_log(self):
        self.append(12)
        before = sorted(p.name for p in self.dir.glob("*.log"))
        dropped = self.compact(8)
        self.assertGreater(dropped, 0)
        base = self.control.verify((self.dir / "base").read_text())
        self.assertEqual(self.control.operations.compacted_through(), base["through_sequence"])
        self.assertLessEqual(base["through_sequence"], 8)
        after = sorted(p.name for p in self.dir.glob("*.log"))
        self.assertEqual(len(after), len(before) - dropped)
        fresh = self.fresh()
        self.assertEqual(fresh.operations.verified_count(self.anchor), 12)
        for number in range(1, base["through_sequence"] + 1):
            self.assertTrue(fresh.operations.consumed("main", f"o{number}"))
        self.assertFalse(fresh.operations.consumed("main", f"o{base['through_sequence'] + 1}"))
        # The log still appends and chains after the compacted prefix.
        fresh.operations.append(
            self.anchor, {"kind": "operation", "tenant": "main", "operation": "o13"}
        )
        self.assertEqual(self.fresh().operations.verified_count(self.anchor), 13)
        # A database older than the compacted prefix cannot be replayed from it.
        self.assertEqual(
            code_of(lambda: self.fresh().operations.records_after(self.anchor, 0)),
            "journal_invalid",
        )

    def test_compaction_stops_at_a_segment_the_caller_keeps_and_never_drops_the_active_one(self):
        self.append(12)
        self.control.operations.compact(self.anchor, 12, lambda ids: ("main", "o3") not in ids)
        self.assertLess(self.control.operations.compacted_through(), 3)
        everything = self.control.operations.compact(self.anchor, 10**9, lambda ids: True)
        self.assertGreater(everything, 0)
        self.assertGreaterEqual(len(list(self.dir.glob("*.log"))), 1)
        self.assertEqual(self.fresh().operations.verified_count(self.anchor), 12)

    def segment_of(self, sequence):
        for path in sorted(self.dir.glob("*.log")):
            for line in path.read_text().splitlines():
                body = self.control.verify(line)
                if body.get("sequence") == sequence:
                    return int(path.name[:8])
        return None

    def test_tampering_with_the_consumed_index_or_the_base_is_refused(self):
        self.append(12)
        self.compact(8)
        index = next(self.dir.glob("consumed-*"))
        original = index.read_bytes()
        index.write_bytes(original + b"main sneaky\n")
        self.assertEqual(
            code_of(lambda: self.fresh().operations.verified_count(self.anchor)), "journal_invalid"
        )
        index.write_bytes(original)
        self.assertEqual(self.fresh().operations.verified_count(self.anchor), 12)
        base = self.dir / "base"
        base.write_text(base.read_text()[:-3] + "000")
        self.assertEqual(
            code_of(lambda: self.fresh().operations.verified_count(self.anchor)), "invalid_token"
        )

    def test_an_interrupted_compaction_leaves_a_log_that_still_verifies(self):
        self.append(12)
        real_unlink = Path.unlink
        calls = []

        def crash(path, *args, **kwargs):
            if path.suffix == ".log":
                calls.append(path.name)
                raise OSError("crash while deleting segments")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", crash):
            with self.assertRaises(OSError):
                self.compact(8)
        self.assertTrue(calls)
        # The base is already written, the old segments still exist: they are ignored.
        fresh = self.fresh()
        self.assertEqual(fresh.operations.verified_count(self.anchor), 12)
        stale = len(list(self.dir.glob("*.log")))
        self.assertEqual(self.compact(8), 0)
        self.assertLess(len(list(self.dir.glob("*.log"))), stale)  # leftovers swept
        self.assertEqual(self.fresh().operations.verified_count(self.anchor), 12)


if __name__ == "__main__":
    unittest.main()
