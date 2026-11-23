"""Can a reader tell the records about the thing asked from the near-identical others?

A mechanical measure over the text a view put in front of a model (the prompt hook, a `recall`),
defined before the display of P2-14 was changed and used unchanged before and after. No agent and
no model: the "reader" is a script. It uses the generator's knowledge of the family (the asked
component, the other components, the attribute words and the pool values) to *score* the text;
nothing here is ever used to build a query or to change what a product shows.

Per shown item (a numbered entry of the list, in the order shown):

* a *statement* is a sentence of the item that names an attribute of one of the family's slots
  (`default listen port`) and states one of that slot's pool values; its *subject* is the
  component the sentence names (the asked one, one of the family's other components, or none)
  and it is *rejected* when its own wording says the value was tried and reverted, suggested and
  not adopted, or marked a draft or a TODO (the generator's three forms of a near miss);
* the item is **asked** when some statement names the asked component, **other** when it has
  statements and none names the asked one, **none** when it has no statement.

Per task, from the shown text:

* `other_share`: items of kind other, over all items shown (what a reader has to look past);
* `needle_rank`: the place of the first item holding a needle sentence, and `other_before` the
  items of kind other that stand before it;
* per slot, the scripted reader's reading of the asked component's statements of that slot:
  `absent` (the needle is not shown), `unique` (every statement names the needle's value),
  `unique_live` (every statement that is not rejected names it), `first_live` (the first
  statement that is not rejected, in the order shown, names it), or `ambiguous` (none of these).

Since P2-14 a list may mark the records that hold the query's words side by side (`holds`):
`marked`, `marked_other` (marked items of kind other) and `marked_needles` (needles carried in a
marked item) count them; they were defined after the first measurements and do not exist for a
list made before the mark.

A reader of this kind is not an agent and these figures are not task quality: they say whether
the shown text *allows* the asked record to be told apart by matching the component, which is
necessary and not sufficient.
"""

from __future__ import annotations

import re

from longhaul import families as fam
from longhaul import gen

REJECTED = re.compile(r"tried|did not adopt|\(draft|TODO")
MARK = "holds `"  # what a list puts in the note of a record that holds the query's words together
ITEM = re.compile(r"(?m)^\d+\. \*\*\[g\d+\]\*\* ")


def flat(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


def items(text: str) -> list[str]:
    """The texts of the numbered entries of a rendered list, in the order shown."""
    parts = ITEM.split(text)[1:]
    out = []
    for part in parts:
        part = re.split(r"\n## |\n\n_(?!_)", part)[0]
        out.append(part.strip())
    return out


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in text.split("\n"):
        out.extend(s for s in re.split(r"(?<=[.!?;])\s+(?=[A-Z(])", line) if s.strip())
    return out


def value_of(sentence: str, spec: fam.Slot) -> str | None:
    for value, written in spec.pool:
        if re.search(rf"(?<![\w.]){re.escape(written)}(?![\w])", sentence):
            return value
    return None


def statements(item: str, family: fam.Family) -> list[dict]:
    """The statements of one item, see the module."""
    subjects = (family.subject, *family.other_subjects)
    found = []
    for sentence in sentences(item):
        for spec in family.slots:
            if spec.attr not in sentence:
                continue
            value = value_of(sentence, spec)
            if value is None:
                continue
            named = next((s for s in subjects if s in sentence), None)
            found.append(
                {
                    "slot": spec.name,
                    "subject": named,
                    "value": value,
                    "rejected": bool(REJECTED.search(sentence)),
                }
            )
    return found


def kind(found: list[dict], family: fam.Family) -> str:
    if any(s["subject"] == family.subject for s in found):
        return "asked"
    return "other" if found else "none"


def measure(text: str, task: gen.Task, needles: list[dict]) -> dict:
    """The figures of the module for one shown text of one task."""
    family = fam.F[task.family]
    shown = items(text)
    parsed = [statements(i, family) for i in shown]
    kinds = [kind(p, family) for p in parsed]
    holds = [
        any(flat(s) in flat(i) for n in needles for s in n["sentences"]) and bool(shown)
        for i in shown
    ]
    rank = next((n + 1 for n, h in enumerate(holds) if h), None)
    out = {
        "items": len(shown),
        "asked": kinds.count("asked"),
        "other": kinds.count("other"),
        "none": kinds.count("none"),
        "needle_rank": rank,
        "other_before": kinds[: rank - 1].count("other") if rank else None,
        "slots": {},
        # defined after P2-14 added the `holds` mark: only a list that carries it has these
        "marked": sum(MARK in i for i in shown),
        "marked_other": sum(MARK in i and k == "other" for i, k in zip(shown, kinds, strict=True)),
        "marked_needles": sum(
            any(MARK in i and flat(s) in flat(i) for i in shown for s in n["sentences"])
            for n in needles
        ),
    }
    for needle in needles:
        slot = next(s for s in task.slots if s.name == needle["slot"])
        mine = [
            st
            for p in parsed
            for st in p
            if st["slot"] == slot.name and st["subject"] == family.subject
        ]
        carried = any(flat(s) in flat(i) for i in shown for s in needle["sentences"])
        live = [st["value"] for st in mine if not st["rejected"]]
        values = {st["value"] for st in mine}
        if not carried:
            reading = "absent"
        elif values == {slot.current}:
            reading = "unique"
        elif live and set(live) == {slot.current}:
            reading = "unique_live"
        elif live and live[0] == slot.current:
            reading = "first_live"
        else:
            reading = "ambiguous"
        out["slots"][slot.name] = reading
    return out


def summarize(rows: list[dict]) -> dict:
    """Totals over the tasks' measures of one source."""
    shown = sum(r["items"] for r in rows)
    slots: dict[str, int] = {}
    for r in rows:
        for reading in r["slots"].values():
            slots[reading] = slots.get(reading, 0) + 1
    ranks = [r["needle_rank"] for r in rows if r["needle_rank"]]
    before = [r["other_before"] for r in rows if r["needle_rank"]]
    return {
        "tasks": len(rows),
        "items": shown,
        "other_share": round(sum(r["other"] for r in rows) / shown, 3) if shown else 0.0,
        "asked_share": round(sum(r["asked"] for r in rows) / shown, 3) if shown else 0.0,
        "mean_needle_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
        "mean_other_before": round(sum(before) / len(before), 1) if before else None,
        "readings": dict(sorted(slots.items())),
        "marked": sum(r["marked"] for r in rows),
        "marked_other": sum(r["marked_other"] for r in rows),
        "marked_needles": sum(r["marked_needles"] for r in rows),
    }
