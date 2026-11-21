"""Procedure drafts from repeated recorded experience: an avoid or do procedure, for review.

Checkpoints record what was tried and failed (`failed_attempts`: "do not repeat"), and
outcome records record what failed, what had to be redone and what the accepted result
rested on. When the same action or failure recurs in at least two independent tasks, the
recurrence is evidence worth a reviewer's attention. This engine looks for it and files
an ordinary procedure proposal, with the method `repeated_outcome_v1`:

* an *avoid* procedure from a failed attempt or a failure that recurs across tasks, with
  the checkpoints and outcome records that show it named in the provenance;
* a *do* procedure from a rework step that recurs across tasks whose outcome succeeded.

The proposal goes through the existing procedure lifecycle unchanged: it is reviewed
(`review`), and only then, by a separate decision, activated. Nothing here activates,
accepts or executes anything, and nothing is drafted from a record the caller cannot read.
Evidence spans are observations (the schema cites nothing else): the observations the
checkpoints reference and the accepted-result evidence of the outcome records; a recurrence
with no readable observation behind it is counted and skipped, never invented. An avoid
procedure also needs observations from a successful outcome in the same tasks, because a
procedure's `success_evidence` may not be empty; without one the draft is not filed.
Deterministic and bounded; no model.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from memoryking.adapters.experience import Experience
from memoryking.adapters.experience_records import ExperienceRecords
from memoryking.adapters.retrieval import Retrieval
from memoryking.domain.anchors import anchors_in
from memoryking.domain.local import LocalError

if TYPE_CHECKING:
    from memoryking.adapters.knowledge_store import Knowledge

METHOD = "repeated_outcome_v1"
MIN_TASKS = 2  # independent task streams before a recurrence is drafted
WINDOW = 512  # newest checkpoint streams and outcome records examined
SPAN_BYTES = 256  # how much of a cited observation a span covers
MAX_EVIDENCE = 16
TEXT_LIMIT = 400


def normal(text: str) -> str:
    """The key two statements are the same under: case-folded, punctuation and spacing gone."""
    return " ".join(re.findall(r"[^\W_]+", text.casefold()))


class OutcomeDrafts:
    def __init__(self, knowledge: Knowledge):
        self.k, self.s, self.db = knowledge, knowledge.s, knowledge.db
        self.r = Retrieval(knowledge.s)

    # --- Reading ---------------------------------------------------------------------

    def _span(
        self, who: dict[str, Any], observation: str, at: int, whole: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """A span over (the start of) a readable observation, or the recorded span itself."""
        row = self.db.execute(
            "SELECT * FROM observations WHERE tenant=? AND id=?", (who["tenant"], observation)
        ).fetchone()
        if row is None or not self.s._visible(who, row, at):
            return None
        if whole is not None:
            return whole if whole["content_hash"] == row["blob"] else None
        try:
            text = self.r._blob(row).decode("utf-8")
        except (UnicodeDecodeError, LocalError):
            return None
        end = len(text.encode()[:SPAN_BYTES].decode("utf-8", "ignore").encode())
        if end <= 0:
            return None
        return {
            "observation": observation,
            "start_byte": 0,
            "end_byte": end,
            "content_hash": row["blob"],
        }

    def _entries(self, who: dict[str, Any], project: str, at: int) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for row in self.db.execute(
            "SELECT c.id,c.task,c.stream,c.body,c.readers,c.dependencies,c.created FROM checkpoint_streams s JOIN checkpoints c ON c.tenant=s.tenant AND c.id=s.latest WHERE s.tenant=? AND s.project=? AND c.state='available' AND c.incarnation=? ORDER BY c.created DESC,c.id LIMIT ?",
            (who["tenant"], project, self.s.incarnation, WINDOW),
        ).fetchall():
            if who["principal"] not in json.loads(row["readers"]) or row["body"] is None:
                continue
            body = json.loads(row["body"])
            cited = [
                d["id"]
                for d in json.loads(row["dependencies"])
                if isinstance(d, dict) and d.get("kind") == "observation"
            ]
            for item in body.get("failed_attempts", []):
                entries.append(
                    {
                        "polarity": "avoid",
                        "source": "checkpoint",
                        "id": row["id"],
                        "task": row["task"],
                        "text": item["action"],
                        "outcome": item["outcome"],
                        "condition": item["do_not_repeat_when"],
                        "observations": cited,
                        "spans": {},
                        "success": False,
                        "created": row["created"],
                    }
                )
        latest: dict[tuple[str, str, str], Any] = {}
        for row in self.db.execute(
            "SELECT * FROM experience_records WHERE tenant=? AND project=? AND kind='outcome' AND incarnation=? AND body IS NOT NULL ORDER BY id LIMIT ?",
            (who["tenant"], project, self.s.incarnation, WINDOW * 4),
        ).fetchall():
            key = (row["principal"], row["incarnation"], row["logical_key"])
            if key not in latest or latest[key]["revision"] < row["revision"]:
                latest[key] = row
        records = ExperienceRecords(self.s)
        for row in latest.values():
            if not records.available(who, row, at):
                continue
            body = json.loads(row["body"])
            spans = {s["observation"]: s for s in body["acceptance_evidence"]}
            common = {
                "source": "outcome",
                "id": row["id"],
                "task": body["task"],
                "observations": list(spans),
                "spans": spans,
                "success": body["result"] == "success",
                "created": row["revision"],
            }
            if body["result"] != "success":
                for text in body["failures"]:
                    entries.append(
                        {
                            **common,
                            "polarity": "avoid",
                            "text": text,
                            "outcome": text,
                            "condition": None,
                        }
                    )
            else:
                # Every successful outcome shows what worked, whether or not it needed rework.
                entries.append(
                    {**common, "polarity": "success", "text": "", "outcome": "", "condition": None}
                )
                for text in body["rework"]:
                    entries.append(
                        {
                            **common,
                            "polarity": "do",
                            "text": text,
                            "outcome": text,
                            "condition": None,
                        }
                    )
        return entries

    @staticmethod
    def _groups(entries: list[dict[str, Any]]) -> list[tuple[str, str, list[dict[str, Any]]]]:
        """Entries that say the same thing: the same normalised text, or else the same
        non-empty set of anchors. Anchors alone are never enough when the text differs and
        the anchor sets differ, so a path that appears in many unrelated failures does not
        merge them."""
        by_text: dict[tuple[str, str], list[dict[str, Any]]] = {}
        by_anchor: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in entries:
            key = normal(entry["text"])
            if not key:
                continue
            by_text.setdefault((entry["polarity"], key), []).append(entry)
            anchors = sorted(set(anchors_in(entry["text"])))
            if anchors:
                by_anchor.setdefault((entry["polarity"], "|".join(anchors)), []).append(entry)
        groups = [(p, "text:" + k, v) for (p, k), v in by_text.items()]
        covered = {frozenset(id(e) for e in members) for _, _, members in groups}
        for (polarity, key), members in by_anchor.items():
            if frozenset(id(e) for e in members) not in covered:
                groups.append((polarity, "anchors:" + key, members))
        return sorted(groups, key=lambda g: (g[0], g[1]))

    # --- Drafting --------------------------------------------------------------------

    def draft(
        self, credential: str, who: dict[str, Any], project: str, limit: int, at: int
    ) -> dict[str, Any]:
        entries = self._entries(who, project, at)
        successes = [e for e in entries if e["success"]]
        drafted: list[dict[str, Any]] = []
        skipped: dict[str, int] = {}

        def skip(reason: str) -> None:
            skipped[reason] = skipped.get(reason, 0) + 1

        for polarity, key, members in self._groups(entries):
            tasks = {m["task"] for m in members}
            if len(tasks) < MIN_TASKS:
                continue
            if len(drafted) >= limit:
                skip("limit")
                continue
            failure: dict[str, dict[str, Any]] = {}
            success: dict[str, dict[str, Any]] = {}
            for entry in members:
                target = success if entry["success"] else failure
                for observation in entry["observations"]:
                    span = self._span(who, observation, at, entry["spans"].get(observation))
                    if span is not None:
                        target.setdefault(observation, span)
            if polarity == "avoid":
                # A later successful outcome in one of these tasks shows what happened instead.
                for entry in successes:
                    if entry["task"] in tasks:
                        for observation in entry["observations"]:
                            span = self._span(who, observation, at, entry["spans"].get(observation))
                            if span is not None:
                                success.setdefault(observation, span)
            for observation in list(success):
                failure.pop(observation, None)
            if not success:
                skip("no_success_evidence" if polarity == "avoid" else "no_evidence")
                continue
            spans = [*success.values(), *failure.values()][:MAX_EVIDENCE]
            kept = {s["observation"] for s in spans}
            latest = max(members, key=lambda m: (m["created"], m["id"]))
            statement = " ".join(latest["text"].split())[:TEXT_LIMIT]
            logical_key = "proc-" + hashlib.sha256(f"{polarity}|{key}".encode()).hexdigest()[:40]
            version = self._version(who["tenant"], project, logical_key, kept)
            if version is None:
                skip("already_proposed")
                continue
            body = self._body(
                polarity, statement, latest, members, tasks, spans, success, failure, kept, who
            )
            args = {
                "project": project,
                "logical_key": logical_key,
                "expected_version": version,
                "body": body,
            }
            try:
                Experience(self.s).proposal(who, args, at)
                result = self.k.propose(credential, who, args, at)
            except LocalError:
                skip("refused_by_store")
                continue
            drafted.append(
                {
                    "revision": result["revision"],
                    "family": result["family"],
                    "polarity": polarity,
                    "tasks": len(tasks),
                    "text": body["text"],
                }
            )
        return {
            "drafted": drafted,
            "skipped": dict(sorted(skipped.items())),
            "entries": len(entries),
            "method": METHOD,
            "coverage": f"newest_{WINDOW}_checkpoint_streams_and_outcome_records; same_text_or_same_anchors_in_{MIN_TASKS}_tasks; drafts_are_procedure_proposals_awaiting_review",
        }

    def _version(self, tenant: str, project: str, logical_key: str, kept: set[str]) -> int | None:
        """The family's version, or None when its newest revision already cites this evidence."""
        family = self.db.execute(
            "SELECT id,version FROM families WHERE tenant=? AND project=? AND logical_key=?",
            (tenant, project, logical_key),
        ).fetchone()
        if family is None:
            return 0
        latest = self.db.execute(
            "SELECT id,purged FROM revisions WHERE tenant=? AND family=? ORDER BY created DESC LIMIT 1",
            (tenant, family["id"]),
        ).fetchone()
        if latest is not None and not latest["purged"]:
            known = {
                row["observation"]
                for row in self.db.execute(
                    "SELECT observation FROM evidence_spans WHERE tenant=? AND revision=?",
                    (tenant, latest["id"]),
                ).fetchall()
            }
            if kept <= known:
                return None
        return int(family["version"])

    @staticmethod
    def _body(
        polarity: str,
        statement: str,
        latest: dict[str, Any],
        members: list[dict[str, Any]],
        tasks: set[str],
        spans: list[dict[str, Any]],
        success: dict[str, Any],
        failure: dict[str, Any],
        kept: set[str],
        who: dict[str, Any],
    ) -> dict[str, Any]:
        avoid = polarity == "avoid"
        condition = latest.get("condition") or "the circumstances are unchanged"
        steps = [f"Do not: {statement}"] if avoid else [statement]
        procedure = {
            "prerequisites": [
                f"The situation matches a recorded failure (do not repeat when: {condition})"
                if avoid
                else "The situation matches earlier tasks that needed this step"
            ],
            "steps": steps,
            "verification": ["Confirm the task's own acceptance checks pass without this step"]
            if avoid
            else ["Confirm the task's own acceptance checks pass after this step"],
            "expected_artifacts": ["A recorded outcome citing evidence"],
            "exceptions": [],
            "failure_signals": [" ".join(str(latest["outcome"]).split())[:TEXT_LIMIT]],
            "recovery": ["Stop, record the outcome in a checkpoint and choose another approach"],
            "known_conditions": [
                f"Drafted from {len(members)} recorded occurrences in {len(tasks)} separate tasks; not reviewed"
            ],
            "success_evidence": sorted(o for o in success if o in kept),
            "failure_evidence": sorted(o for o in failure if o in kept),
            "failure_assessment": (
                f"Recurred in {len(tasks)} separate tasks; whether it recurs elsewhere is unknown"
            ),
            "dependency_mode": "independent",
        }
        return {
            "text": f"{'Avoid' if avoid else 'Do'}: {statement}",
            "kind": "procedure",
            "selector": {"selector_version": "1", "any_of": [{"all_of": [{"op": "any"}]}]},
            "valid_start": None,
            "valid_end": None,
            "task": None,
            "run": None,
            "readers": None,
            "evidence": spans,
            "authority": None,
            "rule_key": None,
            "effect": None,
            "rule_value": None,
            "provenance": {
                "method": METHOD,
                "polarity": polarity,
                "tasks": len(tasks),
                "occurrences": len(members),
                "checkpoints": sorted({m["id"] for m in members if m["source"] == "checkpoint"})[
                    :8
                ],
                "outcomes": sorted({m["id"] for m in members if m["source"] == "outcome"})[:8],
                "drafted_by": who["principal"],
            },
            "procedure": procedure,
        }
