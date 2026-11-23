"""Offline diagnostic: where does a needle rank in the candidate pool, and what outranks it?

No agent and no model. For each task of a seeded group it opens the seeded store in this process
(the service runs on a copy, as `hookprobe` does), asks for the briefing the prompt-time hook asks
for (the harness's scope sentence and the task prompt as the query, floor and limit as the hook
sets them), a careful agent's query and the two-step reader's query (`hookprobe`), and reads the
ranked list the retrieval holds just before the page is cut. For each needle (the sentence found
as `hookprobe` finds it) it reports:

  in_pool      whether any record holding the needle sentence is among the scored candidates,
  rank         its position in that list (1 is the best match),
  scored       how many candidates were scored (256 is the window's size),
  above        what the records ranked above it are about, by the generator's own knowledge of the
               task, never used to build a query: A same component and attribute, B same component
               and another attribute, C another component and the same attribute, D neither,
  shown        whether the text of that query carried the needle sentence.

This reads the retrieval's internals (it wraps the step before the cut), so it is a diagnostic for
this code and not a measure of a product surface. It says nothing about real-task quality.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "benchmarks") not in sys.path:
    sys.path.insert(0, str(ROOT / "benchmarks"))

from longhaul import families as fam  # noqa: E402
from longhaul import gen, hookprobe  # noqa: E402
from longhaul.systems import load as load_system  # noqa: E402
from memoryking import hooks, host_hooks  # noqa: E402
from memoryking.adapters import retrieval  # noqa: E402
from memoryking.adapters.local_ipc import Server  # noqa: E402
from memoryking.adapters.local_store import LocalStore  # noqa: E402

QUERIES = ("hook", "careful", "two_step")


def kind_of(text: str, subject: str, attribute: str) -> str:
    """What a record is about, in the sense of the module docstring: A, B, C or D."""
    found: set[str] = set()
    for sentence in hookprobe.sentences(text):
        low = sentence.lower()
        has_subject, has_attribute = subject.lower() in low, attribute.lower() in low
        found.add(
            "A"
            if has_subject and has_attribute
            else "B"
            if has_subject
            else "C"
            if has_attribute
            else "D"
        )
    return next((k for k in "ABC" if k in found), "D")


class Reader:
    """Holds what the retrieval ranked for the last briefing it served."""

    def __init__(self) -> None:
        self.ranked: list[tuple[int, str, str]] = []
        self._present = retrieval.Retrieval._present

    def install(self) -> None:
        reader = self

        def present(self, who, ranked, limit, positions, families, *rest):  # type: ignore[no-untyped-def]
            reader.ranked = [(s, r, g[0].get("text", "")) for s, r, g in ranked]
            return reader._present(self, who, ranked, limit, positions, families, *rest)

        retrieval.Retrieval._present = present  # type: ignore[method-assign]

    def remove(self) -> None:
        retrieval.Retrieval._present = self._present  # type: ignore[method-assign]


def observation_texts(store: LocalStore, project: str) -> dict[str, str]:
    """Id to text of every live observation of a project (to find the records holding a needle)."""
    out: dict[str, str] = {}
    reader = retrieval.Retrieval(store)
    for row in store.db.execute(
        "SELECT * FROM observations WHERE project=? AND deleted=0", (project,)
    ).fetchall():
        out[row["id"]] = reader._blob(row).decode()
    return out


def probe_group(group: gen.Group, seeded: Path, work: Path) -> list[dict]:
    """One row per needle and query of the group, from a copy of the seeded store."""
    root = work / f"pool-g{group.index}"
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(seeded, root, symlinks=True)
    system = load_system("memoryking")
    system.prepare(root)
    store = LocalStore(system.data, system.control)
    server = Server(store)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    reader = Reader()
    reader.install()
    sessions = {s.id: s for s in group.sessions}
    rows: list[dict] = []
    try:
        for task in group.tasks:
            family = fam.F[task.family]
            where = {
                "socket": system.socket,
                "credential": system._credential(task.scope),
                "project": task.scope.project,
            }
            texts = observation_texts(store, task.scope.project)
            path = hookprobe.named_file(task.prompt, task.repo)
            queries = {
                "hook": hookprobe.SCOPE_LINE + "\n\n" + task.prompt,
                "careful": hookprobe.careful_query(task.prompt),
                "two_step": hookprobe.two_step_query(task.prompt, task.repo[path] if path else ""),
            }
            for needle in hookprobe.needle_sentences(task, sessions):
                holding = {
                    i
                    for i, t in texts.items()
                    if any(hookprobe.flat(s) in hookprobe.flat(t) for s in needle["sentences"])
                }
                attribute = gen.slot_of(family, needle["slot"]).attr
                for name in QUERIES:
                    if name == "hook":
                        shown = hookprobe.carried(
                            host_hooks.prompt({"prompt": queries[name], "host": "claude"}, **where),
                            [needle],
                        )[0]
                        packet = hooks.recall(
                            host_hooks.scrub(queries[name][:2048])[0],
                            budget=host_hooks.PROMPT_FETCH,
                            text=False,
                            limit=host_hooks.PROMPT_LIMIT,
                            min_score_ppm=host_hooks.PROMPT_FLOOR,
                            **where,
                        )
                    else:
                        packet = hooks.recall(queries[name][:2048], budget=6000, text=True, **where)
                        shown = hookprobe.carried(str(packet.get("text", "")), [needle])[0]
                    ranked = reader.ranked
                    rank = next(
                        (n for n, (_, ref, _) in enumerate(ranked, 1) if ref in holding), None
                    )
                    above: dict[str, int] = {}
                    for _, _, text in ranked[: (rank - 1) if rank else len(ranked)]:
                        k = kind_of(text, family.subject, attribute)
                        above[k] = above.get(k, 0) + 1
                    rows.append(
                        {
                            "task": task.id,
                            "slot": needle["slot"],
                            "query": name,
                            "in_pool": rank is not None,
                            "rank": rank,
                            "scored": len(ranked),
                            "above": above,
                            "shown": shown,
                        }
                    )
    finally:
        reader.remove()
        server.shutdown()
        server.server_close()
        store.close()
        system.close()
        shutil.rmtree(root, ignore_errors=True)
    return rows


def summarize(rows: list[dict]) -> dict:
    """Per query: needles, shown, absent from the pool, median rank of the ones present."""
    out: dict[str, dict] = {}
    for name in QUERIES:
        mine = [r for r in rows if r["query"] == name]
        ranks = sorted(r["rank"] for r in mine if r["rank"])
        out[name] = {
            "needles": len(mine),
            "shown": sum(r["shown"] for r in mine),
            "not_in_pool": sum(1 for r in mine if not r["in_pool"]),
            "pool_full": sum(1 for r in mine if r["scored"] >= 256),
            "median_rank": ranks[len(ranks) // 2] if ranks else None,
        }
    return out


def render(summary: dict) -> str:
    lines = ["query     needles  shown  not in pool  pool full  median rank"]
    for name, s in summary.items():
        lines.append(
            f"{name:<9} {s['needles']:>6} {s['shown']:>6} {s['not_in_pool']:>11} {s['pool_full']:>10}"
            f"  {s['median_rank']}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--size", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--seeded", required=True, help="the directory `seed` wrote for the arm")
    parser.add_argument("--work", default="/tmp/mk-lh/poolprobe")
    parser.add_argument("--json", help="write every row to this file")
    args = parser.parse_args()
    suite = gen.build("T1", args.size, args.seed)
    found: list[dict] = []
    for g in suite.groups:
        found += probe_group(
            g, Path(args.seeded) / "memoryking" / f"T1-{args.size}-g{g.index}", Path(args.work)
        )
    if args.json:
        Path(args.json).write_text(json.dumps(found, indent=1))
    print(render(summarize(found)))
