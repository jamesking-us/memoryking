"""The three host hooks against a disposable local service: save, save, then load.

Plays what a host does: a pre-compaction hook captures the transcript tail and
checkpoints the task, a session-end hook captures the last message, and the next
session's start hook prints the resume packet and a cited briefing. Each hook fires
twice to show that a repeated hook replays instead of duplicating. The installer is
exercised in print mode against a scratch directory; nothing outside it is written.
"""

import json
import tempfile
import threading
from pathlib import Path

from memoryking import host_hooks
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore

TRANSCRIPT = [
    {"type": "user", "message": {"role": "user", "content": "Make the retry deadline per request"}},
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Scoped the retry deadline per request in payments/retry.py; "
                    "the regression test now passes.",
                }
            ],
        },
    },
]


def exercise(where: dict, transcript: Path) -> dict:
    transcript.write_text("".join(json.dumps(line) + "\n" for line in TRANSCRIPT))
    compact = host_hooks.read_event(
        json.dumps(
            {
                "session_id": "demo-session",
                "transcript_path": str(transcript),
                "hook_event_name": "PreCompact",
                "trigger": "auto",
                "prompt_id": "p",
            }
        ),
        {},
    )
    first = host_hooks.save(compact, "pre-compact", task="retry-deadline", **where)
    again = host_hooks.save(compact, "pre-compact", task="retry-deadline", **where)
    assert (
        first["observation"] == again["observation"] and first["checkpoint"] == again["checkpoint"]
    )
    ended = host_hooks.read_event(
        json.dumps(
            {
                "session_id": "demo-session",
                "hook_event_name": "Stop",
                "model": "demo",
                "turn_id": "t",
                "last_assistant_message": "Stopped after merging the per-request deadline.",
            }
        ),
        {},
    )
    end = host_hooks.save(ended, "session-end", **where)
    text = host_hooks.session_start(
        host_hooks.read_event(
            json.dumps({"hook_event_name": "SessionStart", "source": "startup"}), {}
        ),
        query="retry deadline",
        task="retry-deadline",
        **where,
    )
    assert "# Resume: Session demo-session" in text and "# Briefing: retry deadline" in text
    return {
        "compact_observation": first["observation"],
        "compact_checkpoint": first["checkpoint"],
        "replayed": again["revision"] == first["revision"],
        "end_observation": end["observation"],
        "start_chars": len(text),
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mk-hooks-", dir="/tmp") as directory:
        root = Path(directory)
        credential = LocalStore.initialize(root / "db", root / "ctl")
        with LocalStore(root / "db", root / "ctl") as store, Server(store) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                where = {"socket": server.path, "credential": credential, "project": "demo"}
                result = exercise(where, root / "transcript.jsonl")
                installs = {
                    host: host_hooks.install(
                        host=host,
                        scope="project",
                        project="demo",
                        cwd=root / "repo",
                        home=root / "home",
                        profile="home",
                        task="retry-deadline",
                        write=False,
                    )["path"]
                    for host in host_hooks.HOSTS
                }
                print("Hooks replayed: one observation and one checkpoint per moment.")
                print(json.dumps({**result, "install_paths": installs}, sort_keys=True))
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
