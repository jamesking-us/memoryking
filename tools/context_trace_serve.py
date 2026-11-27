"""Run the daemon of `tools/context_coverage.py` with a recorder around its own pipeline.

`python tools/context_trace_serve.py serve --store ... --control ...` is `python -m memoryking
serve` plus a wrapper, in this process only, around five functions of the retrieval path:
`_present` (the fused candidate list, in its order), `_diversify` (what survived folding),
`_spread` (the order after presentation), `_beside` (what was chosen, with the neighbouring
turns) and `pack_rendered` (what the packer kept). After each briefing the last record is written
to the file named by `CONTEXT_COVERAGE_TRACE` (a variable that only the coverage tool sets, for
the daemon it starts). Nothing is read twice and no ranking is repeated: the record is the
pipeline's own lists. The product code carries no switch for this.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryking.adapters import retrieval as R  # noqa: E402
from memoryking.domain import briefing as B  # noqa: E402

DUMP = os.environ.get("CONTEXT_COVERAGE_TRACE")
state: dict = {}


def head(text: str) -> str:
    return " ".join(text.split()).lower()[:60]


def row(rank: int, entry) -> dict:
    items = entry[2]
    first = items[0]
    return {
        "ref": entry[1],
        "rank": rank,
        "fused": entry[0],
        "single": len(items) == 1 and first["authority"] == "observed_episode",
        "lex": max(i.get("lexical_score_ppm", 0) for i in items),
        "dense": max((i.get("dense_score_ppm", 0) for i in items), default=0),
        "has_dense": any("dense_score_ppm" in i for i in items),
        "source_bytes": first.get("source_bytes"),
        "start": first.get("excerpt_start_byte"),
        "end": first.get("excerpt_end_byte"),
        "head": head(first.get("text", "")),
    }


def refs(entries) -> list[str]:
    return [e[1] for e in entries]


def wrap_present(original):
    def present(self, who, ranked, *args, **kwargs):
        state.clear()
        state["ranked"] = [row(rank, e) for rank, e in enumerate(ranked, 1)]
        return original(self, who, ranked, *args, **kwargs)

    return present


def wrap_diversify(original):
    def diversify(entries, count):
        state["folded"] = refs(entries)
        return original(entries, count)

    return diversify


def wrap_spread(original):
    def spread(entries, need, count):
        state["diversified"] = refs(entries)
        out = original(entries, need, count)
        state["spread"] = refs(out)
        state["need"] = need
        return out

    return spread


def wrap_beside(original):
    def beside(self, who, chosen, *args, **kwargs):
        out = original(self, who, chosen, *args, **kwargs)
        state["chosen"] = refs(chosen)
        state["beside"] = [row(0, e) for e in out[len(chosen) :]]
        return out

    return beside


def wrap_pack(original):
    def pack_rendered(*args, **kwargs):
        raw, used, selected, omitted = original(*args, **kwargs)
        state["packed"] = [
            {"ref": i.get("reference"), "start": i.get("excerpt_start_byte"), "end": i.get("excerpt_end_byte")}
            for i in selected
        ]
        state["omitted"] = omitted
        if DUMP:
            Path(DUMP).write_text(json.dumps(state))
        return raw, used, selected, omitted

    return pack_rendered


R.Retrieval._present = wrap_present(R.Retrieval._present)
R.Retrieval._diversify = staticmethod(wrap_diversify(R.Retrieval._diversify))
R.Retrieval._spread = staticmethod(wrap_spread(R.Retrieval._spread))
R.Retrieval._beside = wrap_beside(R.Retrieval._beside)
B.pack_rendered = wrap_pack(B.pack_rendered)

if __name__ == "__main__":
    from memoryking.__main__ import main

    sys.argv[0] = "memoryking"
    raise SystemExit(main())
