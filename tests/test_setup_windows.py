"""Windows setup's choices, hook strings and store-only mode, without a Windows machine.

The end-to-end run (WSL2 or Docker, real hooks through Git Bash and PowerShell) is
`memoryking setup claude-code` followed by `memoryking doctor --replay-hooks` on Windows;
here the parts that decide what it does are checked on every platform, and the store side
it drives (`setup --store-only`) on POSIX through a real process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memoryking import hook_check, setup_windows
from memoryking.adapters import platform_support
from memoryking.domain.local import LocalError


class ChooseTests(unittest.TestCase):
    def choose(self, distros: list[tuple[str, int]], engine: bool, wanted: str | None = None):
        with (
            mock.patch.object(setup_windows, "wsl_distros", return_value=distros),
            mock.patch.object(setup_windows, "docker_engine", return_value=engine),
        ):
            return setup_windows.choose(wanted)

    def refused(self, code: str, *args: object) -> LocalError:
        with self.assertRaises(LocalError) as caught:
            self.choose(*args)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_the_one_back_end_there_is_is_used(self) -> None:
        self.assertEqual(self.choose([("Ubuntu", 2)], False)[:2], ("wsl", "Ubuntu"))
        self.assertEqual(self.choose([], True)[:2], ("docker", "memoryking"))

    def test_several_are_never_guessed_between(self) -> None:
        refused = self.refused("store_backend_ambiguous", [("Ubuntu", 2), ("Debian", 2)], True)
        self.assertEqual(refused.detail["found"], ["wsl:Ubuntu", "wsl:Debian", "docker:memoryking"])
        self.assertEqual(self.choose([("Ubuntu", 2)], True, "docker")[:2], ("docker", "memoryking"))
        self.assertEqual(
            self.choose([("Ubuntu", 2), ("Debian", 2)], False, "wsl:Debian")[:2], ("wsl", "Debian")
        )
        self.refused("store_backend_ambiguous", [("Ubuntu", 2), ("Debian", 2)], False, "wsl")

    def test_nothing_usable_says_what_to_install_and_installs_nothing(self) -> None:
        refused = self.refused("store_backend_missing", [("Legacy", 1)], False)
        self.assertIn("wsl", refused.detail["install"])
        self.assertIn("docker", refused.detail["install"])
        self.refused("store_backend_missing", [("Legacy", 1)], False, "wsl:Legacy")
        self.refused("store_backend_missing", [("Ubuntu", 2)], False, "docker")
        self.refused("relay_invalid", [("Ubuntu", 2)], False, "ssh:Ubuntu")


class HookStringTests(unittest.TestCase):
    def test_words_both_windows_shells_read_the_same(self) -> None:
        for word in ("--relay", "wsl:Ubuntu", "C:/Users/ABCDEF~1/py.exe", '"env=dev box"'):
            self.assertTrue(platform_support.neutral_word(word), word)
        for word in ("$HOME", '"a$b"', '"a`b"', "a,b", "~/x", '"a\\b"', "C:\\x", "a;b", "@x"):
            self.assertFalse(platform_support.neutral_word(word), word)

    def config(self, command: str) -> dict[str, object]:
        return {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}

    def test_setup_refuses_a_hook_string_that_would_read_differently(self) -> None:
        setup_windows.neutral(self.config("C:/py.exe -m memoryking.hook_cli hook session-start"))
        with self.assertRaises(LocalError) as caught:
            setup_windows.neutral(self.config('C:/py.exe hook session-start --query "a$HOME"'))
        self.assertEqual(caught.exception.code, "hook_command_unsafe")

    def test_doctor_reads_the_relay_back_from_claude_codes_own_settings(self) -> None:
        base = "C:/py.exe -m memoryking.hook_cli --relay wsl:Ubuntu --relay-profile default-agent"
        config = {
            "hooks": {
                event: [
                    {"hooks": [{"type": "command", "command": f"{base} hook {kind} --project p"}]}
                ]
                for kind, event in hook_check.EVENTS.items()
            }
            | {"Stop": [{"hooks": [{"type": "command", "command": "other-tool"}]}]}
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            found = hook_check.commands(path)
        self.assertEqual(sorted(found), sorted(hook_check.EVENTS))
        self.assertEqual(
            hook_check.relay_of(found["prompt"]),
            {"relay": "wsl:Ubuntu", "relay-profile": "default-agent", "project": "p"},
        )

    def test_the_shell_free_form_carries_the_same_words_and_is_still_ours(self) -> None:
        from memoryking import host_hooks

        line = (
            "C:/PROGRA~1/py.exe -m memoryking.hook_cli --relay wsl:Ubuntu --relay-profile "
            'default-agent hook session-start --host claude --project p --binding "env=dev box"'
        )
        config = self.config(line)
        setup_windows.exec_form(config, "C:\\Program Files\\py.exe")
        (hook,) = config["hooks"]["SessionStart"][0]["hooks"]  # type: ignore[index]
        self.assertEqual(hook["command"], "C:\\Program Files\\py.exe")
        self.assertEqual(hook["args"][-2:], ["--binding", "env=dev box"])
        self.assertEqual(hook["args"][:2], ["-m", "memoryking.hook_cli"])
        self.assertTrue(host_hooks._ours(config["hooks"]["SessionStart"][0]))  # type: ignore[index]
        removed = host_hooks.merge(config, {"SessionStart": []}, remove=True)
        self.assertNotIn("hooks", removed)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            found = hook_check.commands(path)
        self.assertEqual(found["session-start"], [hook["command"], *hook["args"]])
        self.assertEqual(hook_check.relay_of(found["session-start"])["relay"], "wsl:Ubuntu")

    def test_the_form_follows_the_installed_claude_code(self) -> None:
        def version(text: str, code: int = 0):
            done = subprocess.CompletedProcess([], code, text.encode(), b"")
            with (
                mock.patch("shutil.which", return_value="claude.exe"),
                mock.patch.object(setup_windows, "quiet", return_value=done),
            ):
                return setup_windows.claude_version()

        self.assertEqual(version("2.1.330 (Claude Code)\n"), (2, 1, 330))
        self.assertGreaterEqual(version("2.1.330 (Claude Code)"), setup_windows.EXEC_FORM_SINCE)
        self.assertLess(version("2.1.101 (Claude Code)"), setup_windows.EXEC_FORM_SINCE)
        self.assertIsNone(version("garbled"))
        self.assertIsNone(version("2.1.330", code=1))
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(setup_windows.claude_version())

    def test_no_store_side_path_reaches_a_hook_string(self) -> None:
        # Git Bash rewrites a POSIX-looking argument into a Windows path; the store side is
        # named only by its relay and profile.
        argv = [
            "C:/py.exe",
            "-m",
            "memoryking.hook_cli",
            "--relay",
            "wsl:U",
            "--relay-profile",
            "p",
        ]
        self.assertFalse(any(word.startswith("/") for word in argv))


class UpgradeTests(unittest.TestCase):
    def install(self, *replies: bytes) -> tuple[dict[str, str], list[list[str]]]:
        from types import SimpleNamespace

        calls: list[list[str]] = []

        def quiet(argv: list[str], timeout: float = 600) -> subprocess.CompletedProcess[bytes]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, replies[len(calls) - 1], b"")

        say: dict[str, str] = {}
        args = SimpleNamespace(wheel=None, dry_run=False, profile=None)
        with mock.patch.object(setup_windows, "quiet", side_effect=quiet):
            setup_windows.install("wsl", "Ubuntu", args, say)
        return say, calls

    def test_a_new_install_stops_the_daemon_that_ran_the_old_code(self) -> None:
        say, calls = self.install(
            b'{"installed":"now"}\n', b'{"running": false, "stopped": true}\n'
        )
        self.assertEqual(calls[1][-5:], ["-m", "memoryking", "--profile", "default", "stop"])
        self.assertIn("stopped the daemon that ran the earlier install", say["install"])

    def test_the_same_install_leaves_the_daemon_running(self) -> None:
        say, calls = self.install(b'{"installed":"already"}\n')
        self.assertEqual(len(calls), 1)
        self.assertTrue(say["install"].startswith("already installed"))


class UndoRecordTests(unittest.TestCase):
    def test_remove_puts_files_back_byte_for_byte_and_then_the_record_goes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            edited, created = root / "user.json", root / "new" / "settings.json"
            original = b'{\r\n  "theme": "dark"\r\n}\r\n'
            edited.write_bytes(original)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "local")}):
                files = setup_windows.WindowsRecord(dry=False)
                self.assertEqual(files.write(edited, '{"hooks": {}}\n'), "changed")
                self.assertEqual(files.write(created, '{"hooks": {}}\n'), "created")
                files.save()
                record = setup_windows.record_path()
                self.assertTrue(record.is_file())
                self.assertNotIn("credential", record.read_text(encoding="utf-8"))
                again = setup_windows.WindowsRecord(dry=False)
                self.assertEqual(again.restore(edited, lambda text: text), "restored")
                self.assertEqual(again.restore(created, lambda text: text), "deleted")
                again.save_removal()
                self.assertEqual(edited.read_bytes(), original)
                self.assertFalse(created.exists())
                self.assertFalse((root / "new").exists())
                self.assertFalse(record.exists())

    def test_user_variables_are_recorded_and_put_back_unless_changed_since(self) -> None:
        stored: dict[str, str] = {"MEMORYKING_RELAY_PROFILE": "mine"}
        announced: list[int] = []
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.multiple(
                setup_windows,
                read_user_variable=lambda name: stored.get(name),
                write_user_variable=lambda name, value: (
                    stored.pop(name, None) if value is None else stored.__setitem__(name, value)
                ),
                announce_environment=lambda: announced.append(1),
            ),
            mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}),
        ):
            wanted = {"MEMORYKING_RELAY": "wsl:Ubuntu", "MEMORYKING_RELAY_PROFILE": "default"}
            dry = setup_windows.WindowsRecord(dry=True)
            self.assertEqual(sorted(dry.set_environment(wanted)), sorted(wanted))
            dry.save()
            self.assertEqual(stored, {"MEMORYKING_RELAY_PROFILE": "mine"})
            self.assertFalse(setup_windows.record_path().exists())
            files = setup_windows.WindowsRecord(dry=False)
            files.set_environment(wanted)
            files.save()
            self.assertEqual(stored, wanted)
            record = setup_windows.record_path().read_text(encoding="utf-8")
            self.assertNotIn("credential", record)
            again = setup_windows.WindowsRecord(dry=False)
            self.assertEqual(again.set_environment(wanted), [])  # a second run changes nothing
            stored["MEMORYKING_RELAY"] = "docker:memoryking"  # the user changed it since
            undo = setup_windows.WindowsRecord(dry=False)
            self.assertEqual(
                sorted(undo.restore_environment()),
                [
                    "MEMORYKING_RELAY left as it is (changed since setup set it)",
                    "MEMORYKING_RELAY_PROFILE restored",
                ],
            )
            undo.save_removal()
            self.assertEqual(
                stored,
                {"MEMORYKING_RELAY": "docker:memoryking", "MEMORYKING_RELAY_PROFILE": "mine"},
            )
            self.assertFalse(setup_windows.record_path().exists())
            self.assertEqual(len(announced), 2)

    def test_a_dry_run_writes_no_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
                files = setup_windows.WindowsRecord(dry=True)
                files.write(Path(tmp) / "s.json", "{}\n")
                files.save()
                self.assertFalse(setup_windows.record_path().exists())
                self.assertFalse((Path(tmp) / "s.json").exists())


@unittest.skipUnless(
    platform_support.LOCAL_STORE_SUPPORTED, "the store side of a Windows setup runs on POSIX"
)
class StoreOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-sw-")
        self.home = Path(self.temp.name)
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("MEMORYKING_")},
            "HOME": str(self.home),
            "MEMORYKING_SEMANTIC": "off",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.env.pop("PYTHONPATH", None)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.mk, "--profile", "default", "stop")

    def mk(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "memoryking", *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )

    def test_store_only_creates_the_store_and_agent_credential_and_nothing_for_a_host(self) -> None:
        done = self.mk("setup", "claude-code", "--project", "p", "--store-only")
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(result["agent_profile"], "default-agent")
        self.assertTrue((self.home / ".memoryking/default/control/agent.credential").is_file())
        self.assertFalse((self.home / ".claude").exists())
        again = json.loads(self.mk("setup", "claude-code", "--project", "p", "--store-only").stdout)
        self.assertIn("already valid", again["say"]["credential"])

    def test_a_store_on_a_foreign_file_system_is_refused_before_anything_is_made(self) -> None:
        from memoryking import setup_host

        with mock.patch.object(platform_support, "filesystem_type", return_value="9p"):
            with self.assertRaises(LocalError) as caught:
                setup_host.refuse_foreign_filesystem(self.home / "x")
        self.assertEqual(caught.exception.code, "store_filesystem_unsupported")
        self.assertIsNotNone(platform_support.filesystem_type(self.home))
        self.assertNotIn(platform_support.filesystem_type(self.home), {"9p", "drvfs"})


if __name__ == "__main__":
    unittest.main()
