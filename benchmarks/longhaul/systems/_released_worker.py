"""Worker of the arm `memoryking_020`: runs under the Python of the released MemoryKing package (its
own virtualenv) and drives that package, not this checkout's code. It imports `memoryking` first,
refuses to go on unless that is the released 0.2.0 under the virtualenv, and only then makes this
repository's `benchmarks` package importable (appended to the path, so it cannot shadow anything) to
reuse the `memoryking` adapter: the same feeding, the same daemon start, the same briefing and receipt
check as the current arm, with the released library behind them. The caller talks to it in JSON lines on
standard input and output; everything the adapter prints goes to standard error.

Usage: <venv python> -I _released_worker.py <repo root> <expected version> <virtualenv>
"""

import dataclasses
import json
import os
import sys
import traceback
from pathlib import Path


def main() -> int:
    repo, expected, venv = sys.argv[1], sys.argv[2], str(Path(sys.argv[3]).resolve())
    import memoryking

    here = str(Path(memoryking.__file__).resolve())
    if memoryking.__version__ != expected or not here.startswith(venv + os.sep):
        print(
            f"wrong memoryking: version {memoryking.__version__} at {here} "
            f"(want {expected} under {venv})",
            file=sys.stderr,
        )
        return 3
    sys.path.append(repo)
    from benchmarks.longhaul.systems import memoryking as adapter
    from benchmarks.longhaul.systems.base import Scope, Session, Turn

    out = os.fdopen(os.dup(1), "w", buffering=1)
    sys.stdout = sys.stderr  # nothing the adapter prints may corrupt the protocol
    system = adapter.create_semantic()
    system.name = "memoryking_020"
    system.ingest = "turns"

    def reply(obj):
        out.write(json.dumps(obj) + "\n")

    reply({"ready": True, "version": memoryking.__version__, "file": here})
    for line in sys.stdin:
        cmd = json.loads(line)
        try:
            op = cmd["op"]
            if op == "prepare":
                system.prepare(Path(cmd["root"]))
                result = {}
            elif op == "seed":
                sessions = [
                    Session(
                        s["id"],
                        Scope(**s["scope"]),
                        s["started"],
                        tuple(Turn(t["role"], t["text"]) for t in s["turns"]),
                    )
                    for s in cmd["sessions"]
                ]
                result = dataclasses.asdict(system.seed(sessions))
            elif op == "before_retrieval":
                result = system.before_retrieval(Scope(**cmd["scope"]))
            elif op == "context_for":
                text = system.context_for(cmd["query"], Scope(**cmd["scope"]), cmd["budget"])
                result = {"context": text, "note": dict(system.retrieval_note or {})}
            elif op == "close":
                system.close()
                reply({"ok": True, "result": {}})
                return 0
            else:
                raise ValueError(f"unknown op {op!r}")
            reply({"ok": True, "result": result})
        except Exception:  # reported to the caller, which decides
            reply({"ok": False, "error": traceback.format_exc()[-1500:]})
    system.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
