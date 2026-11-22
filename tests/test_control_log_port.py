"""The control-log port: the contract every implementation must meet, run against the file
implementation of both profiles, and a check that the store core asks for nothing the port
does not name.

There is no in-memory implementation to run beside them: it would have to reimplement the
signing, hash chaining and anchoring that the journal, anchor, restore and fault-injection
suites hold the one implementation to (see application/control_log.py)."""

import ast
import secrets
import tempfile
import unittest
from pathlib import Path

from memoryking.adapters.local_files import Control, atomic
from memoryking.adapters.shared_control import SharedControl
from memoryking.application.control_log import ControlLog, LockedControlLog, OperationsLog
from memoryking.contracts.canonical import canonical_bytes
from memoryking.domain.local import LocalError

ZERO = "0" * 64


def fresh_anchor():
    return {
        "store": "s",
        "incarnation": "hist",
        "state": "active",
        "frontiers": {},
        "journal_sequence": 0,
        "journal_head": ZERO,
        "operations_sequence": 0,
        "operations_head": ZERO,
        "last_utc": 0,
        "commit_serial": 0,
    }


def code_of(call):
    try:
        call()
    except LocalError as exc:
        return exc.code
    return None


class ControlLogContract:
    """Everything the store core relies on. Subclasses provide `make`."""

    def make(self, path: Path) -> ControlLog:
        raise NotImplementedError

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mk-ctl-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "control"
        path.mkdir(mode=0o700)
        atomic(
            path / "identity.json",
            canonical_bytes(
                {"store": "store_" + secrets.token_hex(4), "key": secrets.token_hex(32)}
            ),
        )
        atomic(path / "journal", b"")
        self.path = path
        self.log = self.make(path)
        self.anchor = fresh_anchor()
        self.log.anchor(self.anchor)

    def test_signed_bodies_verify_and_a_changed_one_does_not(self):
        token = self.log.sign({"kind": "demo", "n": 1})
        self.assertEqual({"kind": "demo", "n": 1}, self.log.verify(token))
        body, mac = token.split(".")
        forged = body + "." + ("0" if mac[0] != "0" else "1") + mac[1:]
        self.assertEqual("invalid_token", code_of(lambda: self.log.verify(forged)))
        self.assertEqual("invalid_token", code_of(lambda: self.log.verify("not a token")))

    def test_the_anchor_reads_back_what_was_written(self):
        self.anchor["commit_serial"] = 7
        self.log.anchor(self.anchor)
        self.assertEqual(self.anchor, self.log.read_anchor())

    def test_appended_records_chain_and_move_the_anchor(self):
        first = self.log.append(self.anchor, {"kind": "grant", "tenant": "t"})
        second = self.log.append(self.anchor, {"kind": "purge", "tenant": "t"})
        self.assertEqual([1, 2], [first["sequence"], second["sequence"]])
        self.assertEqual(ZERO, first["previous"])
        self.assertEqual(self.log.head(1), second["previous"])
        self.assertEqual(2, self.anchor["journal_sequence"])
        self.assertEqual(self.log.head(2), self.anchor["journal_head"])
        self.assertEqual(self.anchor, self.log.read_anchor())

    def test_records_are_read_from_a_position_and_verified(self):
        for kind in ("a", "b", "c"):
            self.log.append(self.anchor, {"kind": kind, "tenant": "t"})
        everything = self.log.journal(self.anchor)
        self.assertEqual([1, 2, 3], [r["sequence"] for r in everything])
        self.assertEqual(everything[1:], self.log.records_after(self.anchor, 1))
        self.assertEqual([], self.log.records_after(self.anchor, 3))
        self.assertEqual(3, self.log.verified_count(self.anchor))

    def test_a_log_that_does_not_match_its_anchor_is_refused(self):
        self.log.append(self.anchor, {"kind": "grant", "tenant": "t"})
        ahead = dict(self.anchor, journal_sequence=2, journal_head="f" * 64)
        self.assertIsNotNone(code_of(lambda: self.log.verified_count(ahead)))
        wrong_head = dict(self.anchor, journal_head="f" * 64)
        self.assertIsNotNone(code_of(lambda: self.log.journal(wrong_head)))

    def test_a_failure_to_apply_is_remembered_without_its_content(self):
        record = self.log.append(self.anchor, {"kind": "grant", "tenant": "t", "secret": "x"})
        self.log.note_apply_failure(record, LocalError("journal_invalid"))
        held = self.log.apply_failure()
        self.assertEqual("journal_invalid", held["error"])
        self.assertNotIn("secret", str(held))
        described = self.log.describe_pending([record], 0, 1)
        self.assertEqual("journal_invalid", described["pending"][0]["last_apply_error"]["error"])
        self.log.clear_apply_failure(1)
        self.assertIsNone(self.log.apply_failure())

    def test_the_operations_log_is_a_second_chain(self):
        operations: OperationsLog = self.log.operations
        first = operations.append(self.anchor, {"kind": "operation", "tenant": "t"})
        second = operations.append(self.anchor, {"kind": "operation", "tenant": "t"})
        self.assertEqual([1, 2], [first["sequence"], second["sequence"]])
        self.assertEqual(self.anchor["operations_sequence"], 2)
        self.assertEqual(operations.head(2), self.anchor["operations_head"])
        self.assertEqual(2, operations.verified_count(self.anchor))
        self.assertEqual([second], operations.records_after(self.anchor, 1))
        self.assertEqual(2, operations.sync(self.anchor).count)
        self.assertEqual(0, operations.compacted_through())
        self.assertFalse(operations.consumed("t", "never"))
        # Nothing is compacted without a retained backup that holds it.
        self.assertEqual(0, operations.compact(self.anchor, 0, lambda names: True))
        # The journal is untouched by operations records.
        self.assertEqual(0, self.anchor["journal_sequence"])


class FileControlTests(ControlLogContract, unittest.TestCase):
    def make(self, path):
        return Control(path)


class SharedControlTests(ControlLogContract, unittest.TestCase):
    def make(self, path):
        return SharedControl(path)

    def test_one_writer_at_a_time(self):
        log: LockedControlLog = self.log  # type: ignore[assignment]
        with log.locked():
            self.assertEqual(0, log.verified_count(self.anchor))
        with log.locked():  # released, so it can be taken again
            pass


def control_attributes(tree):
    """The attribute names a module uses on a control log (`x.control.attr`, `controller.attr`,
    and `x.control.operations.attr`)."""
    found = set()
    operations = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        # `args.control` is the command line's control directory, a path, not a log.
        is_log = (
            isinstance(base, ast.Attribute)
            and base.attr == "control"
            and not (isinstance(base.value, ast.Name) and base.value.id == "args")
        ) or (isinstance(base, ast.Name) and base.id == "controller")
        if is_log:
            found.add(node.attr)
        if (
            isinstance(base, ast.Attribute)
            and base.attr == "operations"
            and isinstance(base.value, (ast.Attribute, ast.Name))
        ):
            inner = base.value
            if (isinstance(inner, ast.Attribute) and inner.attr == "control") or (
                isinstance(inner, ast.Name) and inner.id == "controller"
            ):
                operations.add(node.attr)
    return found, operations


class CallersDependOnThePortTests(unittest.TestCase):
    def test_nothing_outside_the_implementation_reaches_past_the_port(self):
        root = Path(__file__).resolve().parents[1] / "src/memoryking"
        implementation = {"local_files.py", "operations_log.py", "shared_control.py"}
        port = set(ControlLog.__annotations__) | {
            name for name in dir(ControlLog) if not name.startswith("_")
        }
        port |= {name for name in dir(LockedControlLog) if not name.startswith("_")}
        operations_port = {name for name in dir(OperationsLog) if not name.startswith("_")}
        for path in sorted(root.rglob("*.py")):
            if path.name in implementation:
                continue
            used, used_operations = control_attributes(ast.parse(path.read_text()))
            # Some modules keep the control directory (a path) in an attribute of the same name.
            used -= {"iterdir", "is_dir", "exists", "resolve", "mkdir"}
            self.assertEqual(set(), used - port, f"{path.name} reaches past the control-log port")
            self.assertEqual(
                set(), used_operations - operations_port, f"{path.name}: operations-log port"
            )


if __name__ == "__main__":
    unittest.main()
