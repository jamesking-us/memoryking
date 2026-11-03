# Changelog

Every user-visible change, newest first. The **Unreleased** section is the draft of the next release's notes: when a version is tagged, it moves under that version's heading and its text becomes the body of the [release page](https://github.com/jamesking-us/memoryking/releases) entry. Links are absolute so the text reads the same on GitHub, on PyPI and on the release page.

## Unreleased

Everything below is on `main` since 0.1.0a2 and installs from a checkout; the next pre-release will carry it to PyPI and the container image.

### Optional local reranker

A pinned cross-encoder (`cross-encoder/ms-marco-MiniLM-L6-v2`, Apache 2.0) installed with `python -m memoryking.model_install --model reranker` and loaded with `serve --reranker-model DIR` reorders the top 16 fused candidates of a briefing that asks for it (`rerank: "on"`, `recall --rerank`, lean `recall(rerank=true)`). Off unless requested, no egress, never admits or removes a record; each item reports `rerank_score_ppm`, the trace reports the stage, and the recall harness measures it with `--reranker-model`: on the pooled LongMemEval oracle set, session recall at eight 0.576 to 0.682 and MRR 0.477 to 0.633 for about 0.7 s more per briefing. [Reference](https://github.com/jamesking-us/memoryking/blob/main/docs/reference.md#local-reranker) · [Agent guide](https://github.com/jamesking-us/memoryking/blob/main/docs/agent-guide.md#reranking-the-shortlist-with-a-local-cross-encoder)

### Memory health report

`memoryking health --project P`, the **Memory health** section of the dashboard's Jobs & health view, and the `inspection` action `health` say whether memory is paying for itself, from records the store already keeps: the helpful share of rated feedback, briefings that found no relevant memory, observations no retained delivery ever carried, claims that lost their evidence to a purge or were replaced, and how long the oldest proposal has waited for review. Counts and ratios only, no text and no principal, fixed documented thresholds, five one-line signals. [Reference](https://github.com/jamesking-us/memoryking/blob/main/docs/reference.md#memory-health) · [Agent guide](https://github.com/jamesking-us/memoryking/blob/main/docs/agent-guide.md#is-memory-paying-for-itself-the-health-report)

### Memory as files: the Anthropic memory tool and the Markdown vault

`memoryking.memory_tool.MemoryTool` answers all six commands of Anthropic's `memory_20250818` tool from a project, so any Claude API agent can use MemoryKing as its memory backend without prompt changes: directories are sources, files are versioned observations with their provenance on top, claim files are proposals that wait for review, every view is a receipted fetch, and delete or rename are purges that need an administrator. `memoryking vault export|import` writes the same files to a private directory and reads them back as new versions and proposals. Both rest on a new read-only `inspection` action, `catalog`. [Agent guide](https://github.com/jamesking-us/memoryking/blob/main/docs/agent-guide.md#memory-as-files-the-anthropic-memory-tool-and-the-markdown-vault) · [Reference](https://github.com/jamesking-us/memoryking/blob/main/docs/reference.md#memory-tool-and-vault)

### Proposal drafts from repeated evidence

`memoryking draft-proposals --project P`, the `knowledge` action `draft` and a **Draft proposals** button in the Review view fill the review queue without a model: a name that several independent observations share (a path, a ticket, a host, a symbol, an error code, or failing that a rare term) becomes an ordinary proposal citing one evidence span per supporting observation. Nothing it drafts becomes knowledge until a reviewer accepts it. [Agent guide](https://github.com/jamesking-us/memoryking/blob/main/docs/agent-guide.md#drafts-that-never-accept-themselves) · [Reference](https://github.com/jamesking-us/memoryking/blob/main/docs/reference.md#proposal-drafts)

### Entity anchors, opt-in salience and a judge-free recall harness

Exact names in a capture (paths, symbols, tickets, URLs, hosts, error codes) are anchored at capture and found exactly at retrieval; the memory graph links records that share one. Briefings may ask for `salience: "on"` (`recall --salience`), which nudges the order by the caller's own past deliveries and helpful votes and ranks never-used records after active ones without deleting anything. `tools/evidence_recall.py` measures recall against a pooled oracle with no model as judge, and `tools/check.py` gates on a committed sample. [Agent guide](https://github.com/jamesking-us/memoryking/blob/main/docs/agent-guide.md#anchors-the-exact-names-memory-hangs-on) · [Reference](https://github.com/jamesking-us/memoryking/blob/main/docs/reference.md#anchors-and-salience)

### As-of briefings

`brief` accepts `as_of_ms` (`recall --as-of DATE`, the lean tool's `as_of`, the dashboard's **As of** field): the briefing is assembled from the project as it stood at that instant, and its receipt replays byte for byte. [Reference](https://github.com/jamesking-us/memoryking/blob/main/docs/reference.md#as-of-briefings)

### Source trust

`memoryking trust set|list|held|release` and the Review view hold every capture from a source marked untrusted: recorded, replayable and backed up, but invisible to every read, briefing, graph, resume and review until an admin releases it. [Agent guide](https://github.com/jamesking-us/memoryking/blob/main/docs/agent-guide.md#source-trust-holding-captures-you-do-not-yet-trust) · [Reference](https://github.com/jamesking-us/memoryking/blob/main/docs/reference.md#source-trust)

### Influence report

`memoryking influence OBSERVATION --project P` and the **Where did this go** view list every claim, checkpoint, handoff, summary and retained delivery built on one observation, and the principals those reached, from the same dependency graph a purge follows. [Reference](https://github.com/jamesking-us/memoryking/blob/main/docs/reference.md#influence-report)

### Host hooks

`memoryking hooks install` installs session hooks for Claude Code, Codex, Grok or any stdio host that load memory at the start of a session and save it before compaction and at the end; `memoryking hook` is the command they run, and captures from pytest or git use the same path. [Agent guide](https://github.com/jamesking-us/memoryking/blob/main/docs/agent-guide.md#hooks-that-load-and-save-memory-by-themselves)

### Structured plans in the dashboard and the graph

Named plans with phases, prerequisites, completed steps and blockers, ported from the Codex line: `memory_plan` in the MCP catalog, plan listing and detail routes on the dashboard, plans beside checkpoints in Task activity, and plan nodes in the memory graph, which also gained zoom, pan and persistent selection. The file-backed development-record view was retired. [Agent guide](https://github.com/jamesking-us/memoryking/blob/main/docs/agent-guide.md#structured-plans) · [Reference](https://github.com/jamesking-us/memoryking/blob/main/docs/reference.md#dashboard-plans)

### Smaller changes

- Active Memory views renew their read cycle instead of expiring mid-use, and every shared-profile request is bounded by one deadline.
- Storage, agent and web findings from the 2026-11-01 review of the new code were fixed.
- New error code `vault_missing` in the generated [error codes](https://github.com/jamesking-us/memoryking/blob/main/docs/error-codes.md) page.

## 0.1.0a2 (2026-11-01)

Corrects the project description on PyPI: the README's images and links are absolute, so the page renders the emblem and the dashboard screenshot and its links reach the guides. The code is that of 0.1.0a1.

## 0.1.0a1 (2026-11-01)

First tagged release, under the Apache License 2.0, as a pre-release: a local-first memory, checkpoint and recovery service for coding agents, with a dashboard, a lean MCP profile, provider adapters behind an operator egress policy, session summaries, a storage port with SQLite and PostgreSQL implementations, and team features for the shared profile.
