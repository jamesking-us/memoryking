# Long-history benchmark: track T1 at 2,000 sessions on the fixed generator, beside other memory products (2026-11-23, Claude Sonnet via the Claude Code CLI, commit 52aad17)

Track T1 (needle at scale) at 2,000 sessions only, fresh seed 18: 12 tasks, five arms, one repetition: **60 real agent sessions, no probes, no re-runs, none excluded.** Aggregates only; event streams, stores and working directories stayed outside the repository.

**Why this stage exists, and what it is not.** After [the seed-16 comparison](2026-11-23-t1-products-84071da.md) two things changed: the generator no longer lets a distractor say that the needle's own value was tried, reverted or not adopted ([amendment 11 of `RUNNING.md`](../RUNNING.md#protocol-amendments-after-the-pilot); it changes every history, so seed 16 and earlier are not comparable with seed 18), and roadmap item P2-14 changed how MemoryKing lists and cuts records. The two cannot be separated here: there is no arm with the old code or the old histories. One size only; this says nothing about 20 or 200 sessions. One agent, one model, one repetition, 12 tasks: **only large differences can show**; with 12 tasks 6 wins against none is p 0.031 (uncorrected only), 7 gives 0.0156 (Holm over four 0.0625) and 8 gives 0.0078 (Holm 0.031). A pass is a hidden check passed on a small synthetic fixture, not real-task quality, and a seeded history is not a lived one.

**Labels.** `memoryking` is **as captured**; `mcp_memory`, `basic_memory` and `mcp_memory_service` are **written in full** (generous to them, not what a real session would give them); `log` holds the task's project's transcripts under `history/`.

## Products and configuration

As in [the stage S2 record](2026-11-23-s2-tracks-52aad17.md): MemoryKing 0.1.0 at commit 52aad17 installed as `memoryking setup claude-code` does; the Knowledge Graph Memory server (`@modelcontextprotocol/server-memory`) 2.2.1; Basic Memory 1.0.0 with its Claude Code plugin; MCP Memory Service 13.0.0 with its Claude Code plugin (creation times shifted so the sessions keep their spacing and the last is an hour old); raw transcripts as files.

## Design, fixed before the first session ([stage S2b of `RUNNING.md`](../RUNNING.md#stage-s2b-track-t1-at-2000-sessions-on-the-fixed-generator))

- 12 tasks x 5 arms x 1 repetition = 60 sessions (195 with stage S2), seed 18 (not generated or read before the go-ahead), one history of 2,000 sessions seeded once per arm (five seedings), every run on a copy. `sonnet` only, up to four in parallel, a fixed shuffle, a restartable driver with a durable ledger, the same agent flags, allowed tools, isolation and scope sentence as the earlier records. The first eight sessions were read for harness faults only; they are kept.
- **Analysis: that of the seed-16 record, at one size.** Success: `memoryking` against each other arm, exact two-sided sign test paired by task (12 tasks), Holm over four, Wilson 95% intervals. Cost: input tokens per session (uncached plus cache read plus cache write), exact two-sided Wilcoxon signed-rank paired by task, Holm over four, median paired difference; seconds secondary. Failure classification from the event streams (never surfaced, surfaced but not used, wrong value used, task error) with `hookprobe`'s needle matching. No comparison was added after seeing data.

## Isolation evidence

In all 60 streams the init event shows `claude-sonnet-5-5` (Claude Code 2.1.330) and exactly the arm's own tool servers, all connected; every session returned a success result; none timed out; the longest took 45 seconds. Hook events: `memoryking` SessionStart and UserPromptSubmit 12 of 12, Basic Memory SessionStart 12 of 12, MCP Memory Service SessionStart, UserPromptSubmit and Stop 12 of 12. Runs with a denied command: `log` 8 of 12, `mcp_memory` 3, `basic_memory` 2, `memoryking` 1, `mcp_memory_service` 1. The CLI's computed cost was 7.05 US dollars (not a bill). Seeds, the stub pipeline over all 60 runs (0 faults) and the null/oracle validation were checked offline before any session.

## Results

Passes with Wilson 95% intervals (they treat runs as independent). Every failed run has the check's class `missing` (no value of the needle in the code); none was `stale`, `leak`, `acted_on_injection` or `other`.

| Arm | Passed | Wilson 95% | Input tokens, mean (median) | Output tokens | Seconds | Memory chars |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| `memoryking` (as captured) | **12** (100%) | 76% to 100% | 138,610 (148,084) | 1,366 | 12.5 | 35,194 |
| `mcp_memory` (written in full) | 6 (50%) | 25% to 75% | 337,846 (266,301) | 2,791 | 23.6 | 75,867 |
| `basic_memory` (written in full) | 4 (33%) | 14% to 61% | 266,622 (232,226) | 3,036 | 36.4 | 29,297 |
| `mcp_memory_service` (written in full) | 1 (8%) | 1% to 35% | 137,159 (140,756) | 1,556 | 16.8 | 15,689 |
| `log` | 9 (75%) | 47% to 91% | 197,372 (192,714) | 2,018 | 18.2 | 18,158 |

Per task:

| Track | Task | `memoryking` | `mcp_memory` | `basic_memory` | `mcp_memory_service` | `log` |
| --- | --- | --- | --- | --- | --- | --- |
| T1 | t1-batch-export | pass | pass | fail (missing) | fail (missing) | pass |
| T1 | t1-cache-ttl | pass | fail (missing) | pass | fail (missing) | pass |
| T1 | t1-date-style | pass | fail (missing) | pass | fail (missing) | pass |
| T1 | t1-export-delimiter | pass | fail (missing) | fail (missing) | fail (missing) | fail (missing) |
| T1 | t1-id-format | pass | pass | fail (missing) | fail (missing) | pass |
| T1 | t1-log-format | pass | pass | pass | pass | fail (missing) |
| T1 | t1-money-rounding | pass | pass | pass | fail (missing) | pass |
| T1 | t1-net-timeouts | pass | pass | fail (missing) | fail (missing) | fail (missing) |
| T1 | t1-port-config | pass | pass | fail (missing) | fail (missing) | pass |
| T1 | t1-rate-limit | pass | fail (missing) | fail (missing) | fail (missing) | pass |
| T1 | t1-retention | pass | fail (missing) | fail (missing) | fail (missing) | pass |
| T1 | t1-retry-policy | pass | fail (missing) | fail (missing) | fail (missing) | pass |

**Primary, success** (`memoryking` against each other arm; exact two-sided sign test paired by task, 12 tasks, ties dropped; Holm over four):

| Against | `memoryking` only | Other only | Tied | p | p (Holm over 4) |
| --- | ---: | ---: | ---: | ---: | ---: |
| `mcp_memory` (written in full) | 6 | 0 | 6 | 0.0312 | 0.0625 |
| `basic_memory` (written in full) | 8 | 0 | 4 | 0.0078 | **0.0234** |
| `mcp_memory_service` (written in full) | 11 | 0 | 1 | 0.0010 | **0.0039** |
| `log` | 3 | 0 | 9 | 0.2500 | 0.2500 |

**Primary, cost** (input tokens per session, paired by task, 12 pairs, all non-zero; exact two-sided Wilcoxon signed-rank; Holm over four; median paired difference `memoryking` minus the other):

| Against | `memoryking` higher / lower | Median difference (tokens) | p | p (Holm over 4) |
| --- | ---: | ---: | ---: | ---: |
| `mcp_memory` | 0 / 12 | -113,404 (fewer) | 0.0005 | 0.0020 |
| `basic_memory` | 1 / 11 | -131,326 (fewer) | 0.0010 | 0.0029 |
| `mcp_memory_service` | 6 / 6 | -889 | 1.0000 | 1.0000 |
| `log` | 2 / 10 | -40,976 (fewer) | 0.0024 | 0.0049 |

Secondary, agent seconds (same test; the host ran nothing else heavy): `memoryking` lower than each, Holm p 0.0020 (`mcp_memory`), 0.0020 (`basic_memory`), 0.0068 (`mcp_memory_service`), 0.0020 (`log`); medians 9.0, 23.3, 4.9 and 5.0 seconds fewer.

**What is and is not distinguishable.** After Holm correction `memoryking` is ahead of Basic Memory (8 tasks to 0) and of MCP Memory Service (11 to 0). Against the Knowledge Graph Memory server it is 6 to 0, p 0.031 uncorrected and 0.0625 after correction, so not distinguishable by the declared test; against `log` it is 3 to 0 (p 0.25), also not. It used fewer input tokens per session than the Knowledge Graph Memory server, Basic Memory and `log` (median 113,000, 131,000 and 41,000 fewer; each Holm p below 0.005) and about the same as MCP Memory Service, which is also the product that passed least (1 of 12).

## Beside the seed-16 run at 2,000 sessions (descriptive, no test)

Passed of 12. Different histories (the generator changed), different MemoryKing code (P2-14), one repetition each: these differences are **not** changes caused by anything in particular.

| Arm | Seed 16 | Seed 18 | Input tokens, seed 16 | seed 18 |
| --- | ---: | ---: | ---: | ---: |
| `memoryking` | 6 | 12 | 163,091 | 138,610 |
| `mcp_memory` | 2 | 6 | 331,803 | 337,846 |
| `basic_memory` | 5 | 4 | 226,593 | 266,622 |
| `mcp_memory_service` | 1 | 1 | 114,224 | 137,159 |
| `log` | 10 | 9 | 174,416 | 197,372 |

The other arms are within a few tasks of their seed-16 numbers except the Knowledge Graph Memory server (2 to 6). `memoryking` moved from 6 to 12 and from first behind `log` (6 against 10) to level or ahead (12 against 9). Its failures at seed 16 were mostly needles that the history made contradictory or that never surfaced; this record cannot say whether the generator fix or P2-14 accounts for the change.

## Failures, classified

Every failed run is classified from its event stream: **never surfaced** (the needle sentence of a failing slot is in nothing the model was shown), **surfaced but not used**, **wrong value used**, **task error**.

| Arm | Failed | never surfaced | surfaced, not used | wrong value used | task error |
| --- | ---: | ---: | ---: | ---: | ---: |
| `memoryking` | 0 | 0 | 0 | 0 | 0 |
| `mcp_memory` | 6 | 6 | 0 | 0 | 0 |
| `basic_memory` | 8 | 8 | 0 | 0 | 0 |
| `mcp_memory_service` | 11 | 11 | 0 | 0 | 0 |
| `log` | 3 | 3 | 0 | 0 | 0 |

Every failure of every other arm is a needle that the agent's search never returned (or `grep` never showed); none is a wrong value used. `memoryking` had no failure to classify.

## What reached the model in the `memoryking` arm

Needle sentences are the generator's own sentences that state a slot's current value (the definition `hookprobe` uses), compared on letters and digits; 24 needles.

- The prompt hook fired in 12 of 12 sessions and injected 5,721 characters a session on average; it carried 18 of 24 needles (the offline measure on the development seeds said 95 of 120 at this size, 79%; here 75%).
- The agents called `recall` in 8 of 12 sessions, 22 calls in all; the results carried 14 of 24 needles; **every needle (24 of 24) reached the model by one route or the other**, and all 12 tasks passed.

## Seed cost per arm

Seconds from the seeding driver on a host that also ran offline jobs, one seeding each (indications only); model calls zero for every arm.

| Arm | Seed seconds | Store MB |
| --- | ---: | ---: |
| `memoryking` | 75.3 | 15.16 |
| `mcp_memory` | 71.1 | 1.83 |
| `basic_memory` | 2,108.4 | 86.24 |
| `mcp_memory_service` | 194.3 | 13.49 |
| `log` | under 0.1 | 1.29 |

## Harness faults, exclusions and regrades

None found before, during or after the run; no session excluded, re-run, repeated or regraded; no usage limit met. **Sessions used: 60 of 60** (195 of 195 with stage S2). The only harness change since the seed-16 record that touches T1 is the generator fix (amendment 11), made and tested before seed 18 was generated; the MCP Memory Service creation-time change also applies to this stage's seeding of that product.

## Limits of this run

One agent, one model, one CLI, one repetition, 12 tasks, one size, one seed: one draw. Seeded histories, not lived ones; three products seeded by writing every session in full, `memoryking` as captured, so the groups answer different questions. Each other product is installed with default settings and exposed as its documentation says, without tuning: a differently configured product, or an agent told how to search it, could do better. MemoryKing's 12 of 12 is a count on 12 synthetic tasks with a lower Wilson bound of 76%; the earlier 6 of 12 on another seed and other code shows how far a draw can move. Not run: sizes 20 and 200 on this generator, T5, the lived track.
