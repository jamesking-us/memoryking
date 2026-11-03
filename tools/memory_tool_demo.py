"""Anthropic's memory tool over a disposable local service, with no model.

Plays the six commands the way a Claude API agent sends them: list the memory root,
create a note, edit it twice, read it back with line numbers, propose a claim from it,
and see delete refused for an agent credential and honoured for the owner. Every
write lands in the store as an ordinary capture or proposal, so the same session can
export the result as a Markdown vault and read it from disk.
"""

import json
import tempfile
import threading
from pathlib import Path

from memoryking import vault
from memoryking.adapters.local_files import atomic
from memoryking.adapters.local_ipc import Server
from memoryking.adapters.local_store import LocalStore
from memoryking.memory_tool import MemoryTool
from memoryking.sdk import LocalClient

COMMANDS = [
    {"command": "view", "path": "/memories"},
    {
        "command": "create",
        "path": "/memories/demo/notes/acme.md",
        "file_text": "Acme Corp prefers email follow-ups.\n",
    },
    {
        "command": "str_replace",
        "path": "/memories/demo/notes/acme.md",
        "old_str": "email follow-ups",
        "new_str": "email follow-ups within a day",
    },
    {
        "command": "insert",
        "path": "/memories/demo/notes/acme.md",
        "insert_line": 14,
        "insert_text": "Contact: Pat.\n",
    },
    {"command": "view", "path": "/memories/demo/notes/acme.md"},
    {
        "command": "create",
        "path": "/memories/demo/claims/acme-contact.md",
        "file_text": "---\nevidence: [notes/acme]\n---\nContact Acme by email and expect a reply within a day.",
    },
    {"command": "view", "path": "/memories/demo"},
    {"command": "delete", "path": "/memories/demo/notes/acme.md"},
]


def exercise(root: Path) -> dict:
    credential = LocalStore.initialize(root / "db", root / "ctl")
    store = LocalStore(root / "db", root / "ctl")
    server = Server(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        owner = credential.read_text().strip()
        agent_file = root / "agent.credential"
        atomic(agent_file, store.grant(owner, "agent", "demo", ["read", "capture"]).encode())
        agent = MemoryTool(LocalClient(server.path, agent_file, "demo"))
        results = []
        for index, command in enumerate(COMMANDS):
            result = agent.tool_result(f"toolu_{index}", command)
            results.append(result)
            print(f"> {json.dumps(command)}")
            print(result["content"] + ("\n[is_error]" if result.get("is_error") else ""))
            print()
        assert results[-1].get("is_error"), "an agent credential must not delete evidence"
        owner = LocalClient(server.path, credential, "demo")
        report = vault.export(root / "vault", owner)
        print(f"vault: {report['files']}")
        claim = (root / "vault/demo/claims/acme-contact.md").read_text()
        assert claim.endswith("expect a reply within a day."), claim
        # The owner may delete; the claim built on the note goes with it, as a purge does.
        owner_tool = MemoryTool(owner)
        print(owner_tool.execute(COMMANDS[-1]))
        remaining = owner_tool.execute({"command": "view", "path": "/memories/demo/claims"})
        assert "acme-contact" not in remaining, remaining
        print(remaining)
        print()
        return {"passed": True, "results": results, "vault": report}
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="mk-memory-tool-") as temp:
        exercise(Path(temp))
    print("Memory tool demo passed.")


if __name__ == "__main__":
    main()
