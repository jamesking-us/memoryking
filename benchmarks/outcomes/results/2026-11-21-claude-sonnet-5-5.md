# Task-outcome benchmark: first real-agent run (2026-11-21, Claude Sonnet via the Claude Code CLI)

One repetition of all 10 tasks in all 4 arms: 40 runs, 80 agent sessions, no session timed out or exited with an error. Aggregate figures only; transcripts were kept outside the repository.

## Setup

- Agent: Claude Code CLI 2.1.330 in non-interactive mode, model alias `sonnet` (served as `claude-sonnet-5-5`), run on the owner's own signed-in account. "Computed cost" is the CLI's own figure, not a bill. Tokens are input (including cache reads and writes) plus output, both sessions summed.
- Limits, identical in every arm and session: at most 60 turns per session, 15 minutes of wall-clock time per session (enforced by the harness; never reached), up to 4 runs in parallel on one shared host, so timings are noisy.
- One fresh `claude` process per session, started with the task's working directory as its current directory and given exactly the prompt file as its prompt. No resume or continue. Each session got a new empty home directory, deleted afterwards, and a scrubbed environment (only `PATH`, proxy and certificate settings, the arm's `MEMORYKING_*` variables and the new home), so the CLI's per-project transcripts and its project memory could not carry from session 1 to session 2; the CLI's own auto-memory was also switched off. User settings were not loaded, no skills or slash commands, and no MCP servers except the benchmark's own in arm `mcp`.
- Isolation check before the run: a throwaway two-session probe in an empty directory. Session 1 was told a made-up codeword and asked to keep it for later sessions; session 2, started the same way, answered "unknown". As a control, the same two sessions with one shared home directory and auto-memory on: session 2 returned the codeword, because the CLI had written a project memory file under that home. (In the isolated probe, session 1 wrote the codeword to a `CLAUDE.md` in the working directory; session 2 did not load it. Files in the working directory do carry between sessions, which is how arm `notes` works.)
- Working directories had opaque names under `/tmp`, no project instruction file in or above them, and the prompt held nothing but the task text and the arm's closing sentence.
- Agent command template (arm `mcp` adds the last placeholder; the wrapper script is described above and in `RUNNING.md`):

```
outcome_benchmark.py run --task T --arm A --workdir W --timeout 900 --usage-jq '<result line: tokens, total_cost_usd, duration>' \
  --agent-command "run-agent.sh {workdir} {prompt_file} [{mcp_config}]"

claude -p --model sonnet --output-format stream-json --verbose --max-turns 60 \
  --permission-mode acceptEdits --permission-prompts none \
  --setting-sources "" --disable-slash-commands --strict-mcp-config \
  [--mcp-config <mcp.json>  (arm mcp only)] \
  --allowed-tools "Bash(python -m unittest:*),Bash(python3 -m unittest:*),Bash(python:*),Bash(python3:*)\
[,Bash(memoryking:*)  (arm on)][,mcp__memoryking__remember,...recall,...save_progress,...resume,...hand_off  (arm mcp)]" \
  -- "<prompt file contents>"
```

  Edits and reads inside the working directory were allowed by the permission mode; anything else that needed approval (for example copying files to `/tmp` or `rm -rf`) was refused automatically, the same way in every arm.
- Known limits of the setup: the agent could in principle have listed directories outside its working directory (the prompt told it not to; the transcripts show no read of the repository checkout or of that sibling directory; a few sessions made scratch copies under `/tmp`, some of which were refused), and the harness's `<workdir>.bench` sibling directory exists next to the working directory.
- In arm `mcp` the CLI loads MCP tools lazily: they are listed but must be fetched with its tool-search tool before they can be called. This is how the CLI behaves with any MCP server and was left at its default. It matters for the result (see below). The pilot (the first four runs, one task in each arm) was kept: its `mcp` run did not call the memory tools, which was judged agent behavior rather than a driver fault, and no run or arm was repeated afterwards. Whether the `mcp` arm would do better with the tools loaded up front (a host setting) was not measured.

## Per-run table

Tokens and cost are both sessions summed. "Memory used" is from the store's own counts: whether anything was saved by the end of session 1, and whether session 2 received a briefing with content (an empty store returns a briefing saying nothing relevant, which does not count). "Notes left" is the harness's notes-on-disk heuristic for the `off` and `notes` arms.

| Task | Arm | Success | Tokens | Computed cost (USD) | Seconds | Memory used | Notes left |
| --- | --- | :-: | ---: | ---: | ---: | --- | :-: |
| alert-sink | off | fail | 207,112 | 0.13 | 40 | n/a | no |
| alert-sink | notes | pass | 208,103 | 0.14 | 43 | n/a | yes |
| alert-sink | on | fail | 259,830 | 0.14 | 43 | wrote s1: no; briefing with content in s2: no | n/a |
| alert-sink | mcp | fail | 232,752 | 0.13 | 39 | wrote s1: no; briefing with content in s2: no | n/a |
| cache-lock | off | pass | 298,634 | 0.20 | 73 | n/a | no |
| cache-lock | notes | pass | 375,410 | 0.22 | 205 | n/a | yes |
| cache-lock | on | pass | 401,823 | 0.18 | 126 | wrote s1: yes; briefing with content in s2: no | n/a |
| cache-lock | mcp | pass | 431,097 | 0.21 | 160 | wrote s1: no; briefing with content in s2: no | n/a |
| config-aliases | off | pass | 272,909 | 0.17 | 45 | n/a | no |
| config-aliases | notes | fail | 208,328 | 0.13 | 38 | n/a | yes |
| config-aliases | on | pass | 257,404 | 0.13 | 37 | wrote s1: yes; briefing with content in s2: yes | n/a |
| config-aliases | mcp | fail | 205,362 | 0.10 | 30 | wrote s1: no; briefing with content in s2: no | n/a |
| job-states | off | fail | 175,985 | 0.09 | 26 | n/a | no |
| job-states | notes | pass | 182,513 | 0.12 | 36 | n/a | yes |
| job-states | on | pass | 209,212 | 0.13 | 38 | wrote s1: yes; briefing with content in s2: yes | n/a |
| job-states | mcp | fail | 206,560 | 0.11 | 31 | wrote s1: no; briefing with content in s2: no | n/a |
| ledger-match | off | pass | 263,353 | 0.17 | 85 | n/a | no |
| ledger-match | notes | pass | 258,599 | 0.16 | 61 | n/a | yes |
| ledger-match | on | pass | 291,060 | 0.17 | 61 | wrote s1: yes; briefing with content in s2: no | n/a |
| ledger-match | mcp | pass | 179,292 | 0.11 | 45 | wrote s1: no; briefing with content in s2: no | n/a |
| legacy-rounding | off | pass | 246,326 | 0.11 | 34 | n/a | no |
| legacy-rounding | notes | pass | 296,179 | 0.12 | 35 | n/a | no |
| legacy-rounding | on | pass | 304,016 | 0.13 | 40 | wrote s1: no; briefing with content in s2: no | n/a |
| legacy-rounding | mcp | fail | 222,688 | 0.10 | 31 | wrote s1: no; briefing with content in s2: no | n/a |
| partner-feed | off | fail | 192,488 | 0.07 | 23 | n/a | no |
| partner-feed | notes | pass | 227,991 | 0.15 | 48 | n/a | yes |
| partner-feed | on | pass | 316,908 | 0.19 | 62 | wrote s1: yes; briefing with content in s2: yes | n/a |
| partner-feed | mcp | fail | 174,543 | 0.08 | 24 | wrote s1: no; briefing with content in s2: no | n/a |
| retry-helper | off | pass | 245,445 | 0.10 | 28 | n/a | no |
| retry-helper | notes | fail | 221,591 | 0.10 | 30 | n/a | no |
| retry-helper | on | pass | 203,057 | 0.10 | 28 | wrote s1: no; briefing with content in s2: no | n/a |
| retry-helper | mcp | pass | 178,603 | 0.09 | 25 | wrote s1: no; briefing with content in s2: no | n/a |
| staging-collector | off | fail | 169,422 | 0.06 | 21 | n/a | no |
| staging-collector | notes | fail | 179,526 | 0.11 | 34 | n/a | yes |
| staging-collector | on | pass | 289,163 | 0.15 | 48 | wrote s1: yes; briefing with content in s2: yes | n/a |
| staging-collector | mcp | fail | 173,159 | 0.07 | 19 | wrote s1: no; briefing with content in s2: no | n/a |
| wrong-total | off | pass | 372,239 | 0.15 | 49 | n/a | no |
| wrong-total | notes | pass | 274,309 | 0.11 | 33 | n/a | yes |
| wrong-total | on | pass | 331,909 | 0.14 | 44 | wrote s1: yes; briefing with content in s2: yes | n/a |
| wrong-total | mcp | pass | 297,441 | 0.12 | 39 | wrote s1: no; briefing with content in s2: no | n/a |

## Reading it

- Success: `off` 6/10, `notes` 7/10, `on` (CLI) 9/10, `mcp` 4/10. No pair of arms differs beyond chance (smallest p is 0.0625, `on` against `mcp`; nothing is corrected for six comparisons).
- Memory use decided the memory arms. In arm `on`, 7 of 10 runs saved something in session 1 and 5 of 10 received a briefing with content in session 2. In arm `mcp`, no run saved anything in session 1; 3 runs fetched the recall tool in session 2 and got an empty answer. The `mcp` arm therefore measured an agent that did not use memory, not memory that failed to help.
- Four tasks are the ones where knowledge carried from session 1 was needed in practice: `alert-sink`, `job-states` (decisions) and `partner-feed`, `staging-collector` (environment facts). `off` passed none of them. `notes` passed 3 of 4, `on` 3 of 4, `mcp` 0 of 4. On the other six tasks (instructions and failed approaches) `off` already passed 6 of 6 in this run, so those tasks did not separate the arms here. This split was made after seeing the results and is descriptive only.
- A notes file did as well as MemoryKing: `notes` 7/10 against `on` 9/10 is not a distinguishable difference (3 tasks won by `on`, 1 by `notes`, p = 0.625), and on the four discriminating tasks they tie at 3 of 4 (`notes` failed `staging-collector`, `on` failed `alert-sink`, where the agent never saved its knowledge). In 8 of 10 `notes` runs the agent left notes in `NOTES.md`; in `off` no run left notes.
- Failures that look unrelated to memory occurred in single runs (`notes` on `config-aliases` and `retry-helper`, `mcp` on `legacy-rounding`, where `off` passed). With one repetition run-to-run variation cannot be separated from arm effects.

## Limits

One repetition per task and arm, so no estimate of run-to-run variation; ten synthetic tasks, small enough that six of them were passed by the agent with no memory at all; one model, one agent host (the Claude Code CLI) and one machine, run several at a time; the memory arms depended on whether this agent chose to call the tools. Only large effects are distinguishable at this size, and none was found. This is not evidence of real-task quality or production performance.

## Harness report output

The output of `report` for the 40 result files, unedited.

### Task outcomes by arm

| Arm | Runs | Successes | Success rate | 95% interval |
| --- | ---: | ---: | ---: | --- |
| Off (nothing) | 10 | 6 | 60% | 31% to 83% |
| Notes file | 10 | 7 | 70% | 40% to 89% |
| Memory CLI (on) | 10 | 9 | 90% | 60% to 98% |
| Memory MCP | 10 | 4 | 40% | 17% to 69% |

Cost per run, both sessions summed (mean / median; n/a when not recorded):

| Arm | Tokens | Cost (USD) | Wall time (s) |
| --- | ---: | ---: | ---: |
| Off (nothing) | 244,391 / 245,886 | $0.13 / $0.12 | 42.4 / 36.7 |
| Notes file | 243,255 / 224,791 | $0.14 / $0.12 | 56.4 / 37.2 |
| Memory CLI (on) | 286,438 / 290,112 | $0.15 / $0.14 | 52.7 / 43.6 |
| Memory MCP | 230,150 / 205,961 | $0.11 / $0.11 | 44.4 / 31.3 |

| Task | Knowledge kind | Off (nothing) | Notes file | Memory CLI (on) | Memory MCP |
| --- | --- | :-: | :-: | :-: | :-: |
| alert-sink | decision | fail | pass | fail | fail |
| cache-lock | failed_approach | pass | pass | pass | pass |
| config-aliases | instruction | pass | fail | pass | fail |
| job-states | decision | fail | pass | pass | fail |
| ledger-match | failed_approach | pass | pass | pass | pass |
| legacy-rounding | instruction | pass | pass | pass | fail |
| partner-feed | environment | fail | pass | pass | fail |
| retry-helper | instruction | pass | fail | pass | pass |
| staging-collector | environment | fail | fail | pass | fail |
| wrong-total | failed_approach | pass | pass | pass | pass |

| Knowledge kind | Off (nothing) | Notes file | Memory CLI (on) | Memory MCP |
| --- | ---: | ---: | ---: | ---: |
| decision | 0 of 2 | 2 of 2 | 1 of 2 | 0 of 2 |
| environment | 0 of 2 | 1 of 2 | 2 of 2 | 0 of 2 |
| failed_approach | 3 of 3 | 3 of 3 | 3 of 3 | 3 of 3 |
| instruction | 3 of 3 | 1 of 3 | 3 of 3 | 1 of 3 |

Paired comparisons (exact McNemar test: a two-sided binomial sign test on the tasks where exactly one of the two arms passed):

| Arms | Pairs | Only first passed | Only second passed | Both passed | Neither | p | Reading |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Off (nothing) vs Notes file | 10 | 2 | 3 | 4 | 1 | 1.0000 | Notes file passed 3 tasks that Off (nothing) failed, against 2 the other way; that is within what chance produces, so no difference is shown. |
| Off (nothing) vs Memory CLI (on) | 10 | 0 | 3 | 6 | 1 | 0.2500 | Memory CLI (on) passed 3 tasks that Off (nothing) failed, against 0 the other way; that is within what chance produces, so no difference is shown. |
| Off (nothing) vs Memory MCP | 10 | 2 | 0 | 4 | 4 | 0.5000 | Off (nothing) passed 2 tasks that Memory MCP failed, against 0 the other way; that is within what chance produces, so no difference is shown. |
| Notes file vs Memory CLI (on) | 10 | 1 | 3 | 6 | 0 | 0.6250 | Memory CLI (on) passed 3 tasks that Notes file failed, against 1 the other way; that is within what chance produces, so no difference is shown. |
| Notes file vs Memory MCP | 10 | 4 | 1 | 3 | 2 | 0.3750 | Notes file passed 4 tasks that Memory MCP failed, against 1 the other way; that is within what chance produces, so no difference is shown. |
| Memory CLI (on) vs Memory MCP | 10 | 5 | 0 | 4 | 1 | 0.0625 | Memory CLI (on) passed 5 tasks that Memory MCP failed, against 0 the other way; that is within what chance produces, so no difference is shown. |

Did the agent use memory? From the store's own counts: written in session 1 means observations or checkpoints saved by the end of session 1; read in session 2 means briefings or deliveries served during session 2.

| Arm | Runs | Wrote in session 1 | Read in session 2 | No store figures |
| --- | ---: | ---: | ---: | ---: |
| Memory CLI (on) | 10 | 7 | 10 | 0 |
| Memory MCP | 10 | 0 | 3 | 0 |

Notes left on disk in session 1 (new non-code files, edited notes or text files, or Python changes that only add comments or docstrings):

| Arm | Runs | Left notes | Not recorded |
| --- | ---: | ---: | ---: |
| Off (nothing) | 10 | 0 | 0 |
| Notes file | 10 | 8 | 0 |

Ten tasks can only show a large effect: between two arms, six tasks won by one and none by the other is the smallest result that reaches p < 0.05, and a small difference is indistinguishable from chance. With several arms, each comparison is a separate test and nothing here corrects for that. Success here means the hidden check passed for one agent, one model and the runs listed.
