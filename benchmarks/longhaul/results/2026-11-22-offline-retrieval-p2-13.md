# Finding the right record in a very large history, before and after roadmap item P2-13 (offline, scripted readers, no agent)

**What this is.** A scripted measurement, with no agent and no model call of any kind, of which needle sentences of track T1 reach the model's context at 20, 200 and 2,000 seeded sessions, and of what the retrieval holds just before it cuts a list. It says what was put in front of a model, not whether a real agent would have used it, and it is not evidence of real-task quality or of any improvement in an agent's pass rate. The real-agent record of the same hooks, before this change, is [2026-11-22-t1-remeasure-033a9f0.md](2026-11-22-t1-remeasure-033a9f0.md) (seed 13): MemoryKing passed 12 of 12 tasks at 200 sessions and 4 of 12 at 2,000, a grep-able raw log 11 and 8; at 2,000 the prompt hook carried 5 of 24 needles, 5 of 8 failures were "never surfaced" and 3 "surfaced but not used". Nothing here replaces that record. Histories are generated and seeded exactly as the benchmark does (`tools/longhaul_benchmark.py seed`, through the hook path); the measures are `tools/longhaul_benchmark.py hookprobe` (`benchmarks/longhaul/hookprobe.py`), `benchmarks/longhaul/preflight.py` and the new `benchmarks/longhaul/poolprobe.py`.

**Data discipline.** Tuned only on development seeds 1 to 5. Seeds 11, 12 and 13 are used up: none was generated, seeded or probed here and nothing was tuned on them (the real-agent record of seed 13 was read as the statement of the problem, and only for that). Seed 14 was generated and seeded before the code was frozen but not scored; it was scored once, for the original code and for the code as it stood when the retrieval and the hook were frozen. Afterwards three things changed that touch no ranking: the order of two stages in the trace, a few words of the lines under a list and of the lean tool description, and the lean tool dropping its `file` parameter to stay within its catalog size budget. The development seeds were run again on the final tree and carry the same needles in every cell (the tables below are from that run), so seed 14 was not scored a second time. One constraint, not a target, shaped the final constants: the retrieval gates in `tools/check.py` (the LongMemEval regression sample in particular) had to pass with no threshold lowered. Everything is a synthetic generator and a scripted reader.

**The scripted readers.** (a) the prompt hook, the installed `UserPromptSubmit` command run on the task prompt with the harness's scope sentence in front; (b) the start briefing; (c) one `recall` with that prompt as the query (the lean tool's defaults); (d) one `recall` with the query a careful agent writes (the phrase after "settled for" and the words of the function names the prompt gives); (e) a **two-step reader**: step one is what the prompt showed ((a) and (b)); step two is one `recall` whose query is built by a fixed rule (`hookprobe.two_step_query`, with tests) from the careful query plus up to 16 more words from **the text of the repository file the task tells the agent to edit** (identifiers split at underscores and case changes, comment and docstring words, TODO text; no code keywords), never from the history, a needle or an answer; carry after step two is the union of both steps; step three reads on once, as the reply says to: the same query with the next page (`offset` 32). A needle is *carried* when its own sentence (found in the generated history: in a needle session, naming the task's subject and the slot's attribute and stating its current value) is in the text, compared on letters and digits only. In the generated repositories the file the task names is a stub (a function name and a one-line docstring), so step two adds the function's own words and little else: the stub does not name the attribute the history decided, which is also why a real agent has to think of it.

## Step 1: what happens to the needle at 2,000 sessions (before the change)

Read from a pool diagnostic (`benchmarks/longhaul/poolprobe.py`: the store runs in the process, the ranked list is read just before the page is cut; run on the original code) on the five development seeds at 2,000 sessions, 120 needles, with the prompt hook's own query (the harness's scope sentence and the task prompt).

| What was found | Count |
| --- | --- |
| Needles carried by the prompt hook | 51 of 120 |
| Needles **not in the candidate pool at all** | 0 of 120 (the scored pool held 216 records on average; it was full, 256, for 34 of the 120 queries) |
| Rank in the pool of the 69 needles the hook missed | median 45 (range 5 to 107): 10 within the first 32, 42 at 33 to 64, 17 at 65 to 128 |
| What outranked a missed needle (49 records per case on average) | 72% records about the same component and another attribute ("the file encoding of the orders export"); 18% the same component and attribute with another value (a value that was tried, a colleague's suggestion, an earlier value); 4% another component; 6% generic requests |
| How far below the top the needle scored | median 9% (largest 30%); 13 of the 69 tied exactly at the top (1,000,000: the prompt names the file, every record naming it scores alike) |

So at 2,000 sessions the mechanism is neither tokenisation, the floor, nor the pool. The needle is in the pool, within a tenth of the top score, among 40 to 60 records that say the same kind of thing about the same component; the prompt does not name the attribute, no score separates them, and the list showed the first 22 to 25 of them in whatever order the tie fell.

Twelve or more of the 52 failing (task, seed) cases, in full:

| Seed | Task | Slot | Rank of the needle in the pool | Above it: same component, same attribute / same component, other attribute / other component / generic | Score (needle / top) |
| --- | --- | --- | ---: | --- | ---: |
| 1 | batch-export | order | 66 of 256 | 14 / 43 / 1 / 7 | 1,000,000 / 1,000,000 |
| 1 | id-format | prefix | 41 of 256 | 10 / 30 / 0 / 0 | 583,776 / 628,039 |
| 1 | rate-limit | window | 31 of 192 | 2 / 27 / 1 / 0 | 568,805 / 625,849 |
| 2 | batch-export | size | 53 of 256 | 7 / 40 / 2 / 3 | 1,000,000 / 1,000,000 |
| 2 | export-delimiter | delimiter | 69 of 256 | 9 / 57 / 0 / 2 | 560,906 / 613,262 |
| 2 | net-timeouts | connect | 22 of 206 | 5 / 14 / 1 / 1 | 602,819 / 646,015 |
| 2 | retention | days | 37 of 209 | 8 / 20 / 4 / 4 | 418,811 / 462,708 |
| 3 | cache-ttl | key_prefix | 48 of 188 | 9 / 29 / 3 / 6 | 416,426 / 501,679 |
| 3 | id-format | prefix | 25 of 256 | 1 / 23 / 0 / 0 | 594,559 / 637,997 |
| 3 | net-timeouts | connect | 47 of 192 | 13 / 28 / 1 / 4 | 592,475 / 656,667 |
| 3 | retention | archive_dir | 44 of 219 | 7 / 26 / 3 / 7 | 410,224 / 457,619 |
| 4 | cache-ttl | key_prefix | 41 of 168 | 5 / 31 / 2 / 2 | 410,705 / 491,509 |
| 4 | id-format | prefix | 65 of 256 | 13 / 46 / 3 / 2 | 569,395 / 625,530 |
| 4 | port-config | env_prefix | 33 of 176 | 13 / 13 / 5 / 1 | 1,000,000 / 1,000,000 |

Where the needle was shown but not used. Of the 51 needles the old hook did carry, 49 were shown beside at least three other records that name the same attribute. Three of them, as the agent would have seen them (the needle is the first line; the others are from the same list):

*Seed 2, `t1-cache-ttl`, slot `ttl`: shown among 21 records, 16 others name the same attribute.*

> **User: I need a small change in the dead code; nothing big. Assistant: Confirmed outcome of the team call: response cache, time to live in seconds: 1800. (2025-08-21)**  <- the needed record
> Assistant: I tried 5400 for the time to live in seconds of the response cache on a scratch branch, but it broke the importer; reverted. (2025-11-25)
> Assistant: I tried 90 for the time to live in seconds of the response cache on a scratch branch, but it broke the importer; reverted. (2025-11-01)
> User: I need a small change in the typing hints; nothing big. Use 300 as the time to live in seconds for the token cache: that is what the interface we agreed with the other team needs. (2025-10-05)
> User: Final answer from the team on the session cache: the time to live in seconds is 90. (2025-10-04)
> User: The release notes came up in standup again. Compliance says the time to live in seconds of the session cache has to be 5400; treat that as fixed. (2025-09-01)
> User: Could you check the release notes and tell me what you find? Assistant: A colleague suggested 90 as the time to live in seconds for the response cache; we did not adopt it. (2025-05-24)

*Seed 1, `t1-date-style`, slot `date_fmt`: shown among 21 records, 15 others name the same attribute.*

> **User: My preference: month.day.two-digit year, like 03.09.24 as the date format for the report pages. (2025-05-28)**  <- the needed record
> Assistant: A colleague suggested month.day.two-digit year, like 03.09.24 as the date format for the report pages; we did not adopt it. (2025-11-17)
> Assistant: We settled the interface with the other team: the date format for the dashboard tiles is year/month/day with slashes, like 2024/03/09. (2025-11-06)
> User: Rule: the email digests must always use day-abbreviated month-year, like 09-Mar-2024 as the date format. No exceptions. (2025-11-05)
> Assistant: Agreed with the other side: for the email digests, the date format is month.day.two-digit year, like 03.09.24, and the code has to match. (2025-10-31)
> Assistant: A colleague suggested day.month.year with dots, like 09.03.2024 as the date format for the report pages; we did not adopt it. (2025-10-04)
> Assistant: Agreed with the other side: for the dashboard tiles, the date format is year/month/day with slashes, like 2024/03/09, and the code has to match. (2025-08-21)

*Seed 3, `t1-log-format`, slot `level_case`: shown among 22 records, 15 others name the same attribute.*

> **User: My preference: capitalized, like Warn as the level casing for the proxy log lines. (2025-01-08)**  <- the needed record
> User: Please review the typing hints before the end of the week. Assistant: I tried upper case, like WARN for the level casing of the proxy log lines on a scratch branch, but it broke the importer; reverted. (2025-11-21)
> Assistant: A colleague suggested capitalized, like Warn as the level casing for the proxy log lines; we did not adopt it. (2025-11-03)
> User: Can you take a look at the docstrings? Assistant: A colleague suggested lower case, like warn as the level casing for the proxy log lines; we did not adopt it. (2025-09-19)
> Assistant: A colleague suggested upper case, like WARN as the level casing for the proxy log lines; we did not adopt it. That is all for this pass; I did not change behaviour outside edge/logfmt.py. (2025-07-26)
> User: Compliance says the level casing of the worker log lines has to be upper case, like WARN; treat that as fixed. (2025-07-05)
> User: Final answer from the team on the access log lines: the level casing is capitalized, like Warn. (2025-06-27)

What would have let a reader tell the right record from its neighbours is in the sentences themselves and was not made easy to see: the subject ("response cache", not "session cache", "token cache" or "asset cache"), and the kind of statement ("agreed", "confirmed outcome", "we settled" against "I tried ... reverted" and "a colleague suggested ... we did not adopt it"). What made it harder was the order and the line under the list: the records were shown newest first with "where records disagree the later one is current", so the newest line about the same attribute, which is about another component, reads as the current value. A list that is a hundred records about the same component but only a few distinct statements also spends its places on values that were tried and dropped.

A history three times larger leaves the needle out of the pool altogether for a generic query: see "Larger than the benchmark" below.

## What was changed

1. **A spread over a crowd of near-equal records.** The earlier rule (of the best 64 items the first two of any group of near-identical ones keep their place, the rest follow) stays. After it, the places of the whole candidate window (256 records, the newest of the most informative search, unchanged) are filled greedily: each goes to the item that adds most among those that match nearly as well as the best one left (a score of at least 70% of it), where "adds" is the share of the item's own words that no item already placed has. Words that more than 30% of a window of 24 or more items hold (the component's name, a request every session opens with) do not count, and an item that is at least half new adds all it can. Only a crowd is spread: when 16 or fewer items are that close, the order stands, and a clearly better match always comes first. The order is the same for every page of one query. Nothing is dropped. No schema change.
2. **Say what each shown record is about.** A page of four or more observations carries, per record, the files, symbols and tickets it names (the anchors the store already holds from capture), leaving out a name that more than half of the page shares (the day was already shown). In the benchmark's histories every record of a component names the same file, so the label is rarely printed there (about 1 record in 12, on values that look like a path or a host); it is for histories in which records name different files, and costs nothing where they do not.
3. **Narrowing and continuing in the lean `recall` tool**, two optional parameters and no new tool: `offset` (the next page of the same list) and `match: "all"` (only records holding every word of the query; nothing rather than a near miss; to stay within a file, name it in the query). A `brief` also takes `file` (only records naming that file, function or ticket exactly, inside the authorization filter): the lean catalog has a size budget (12,288 bytes) that the parameter did not fit. When more matched than the reply holds, a line under the list says which records these are, how many matched (a floor, "at least", when the window was full), the `offset` for the next page and how to narrow; such a page is packed from the top without gaps so the offset is exact. The tool description and the server's instructions say that a long history shows a sample and to search again with the exact property once the code is read. A text names at most three of the records an item stands for (it named up to sixteen), the receipt still names all.
4. **The prompt hook reads two pages** (it asks for the next 32 when more than 32 matched, inside the same deadline), shows what fits the unchanged 2,000-token budget, leaves out the Sources lines of a list cut to particular sentences (a fifth of the budget), counts what matched, and says exactly how to go on (`offset`, `match`). The line under a list now says that where two records give different values for the same thing the later is current, and that records about different things do not disagree (the earlier wording let a newer record about another component read as the current value).
5. **Fewer comparisons for the same result.** Duplicates are found by bucketing on a record's non-numeric words and excerpts inside another by comparing only longer ones: the result of comparing every pair, pinned by a test on generated data.

## What was tried and dropped

* **Relevance times novelty over the whole window, with no band** (an item's score over the best score times 0.25 plus 0.75 of its new-word share). On the development seeds it carried 88 of 120 needles in the prompt hook at 2,000 sessions against 83 for the shipped rule and helped T2 about as much, but it moved the LongMemEval regression sample's turn recall at 5 from 0.722 to 0.597 and failed the gate in six places. A band alone (0.9, 0.5) failed it in two or three places by 0.003 to 0.008. A band of 0.6 or 0.5 failed one. The shipped band (0.7) and crowd threshold (16) pass with every threshold unchanged; turn recall-all at 10 is 0.681 against a threshold of 0.67 (0.694 before), one question above it, so the constants are at their limit and a change to the band that costs one more question fails the gate.
* **Dropping the older near-identical cap** in favour of the spread alone: with the whole window folded as well, the two-step reader's `recall` carried 77 of 120 needles against 82 before. The cap costs nothing in a list that has no crowd, so it stays.
* **Comparing the whole window for repeats** instead of the best 64: the two-step `recall` fell to 75 of 120 (82 before), because a short excerpt that stood alone became the whole session it lay inside, longer items filled the budget (28 to 30 records against 32) and the comparison cost more time. The best 64 stay.
* **Scoring the spread on the word score alone** instead of the combined score (a prompt that names a file scores every record that names it 1,000,000, so the combined score cannot separate them): 75 of 120 against 88 in the same setting. Records that match many query words but say nothing about the property came first.
* **Other forms of novelty**: an absolute count of new words, a saturating form, the share of new sentences, words weighted by how many records of the pool repeat a sentence. All within a few needles of the shipped one on the pool lists of five seeds (the ranking is saturated: after any spread the best 64 hold 87% to 90% of needles).
* **Cutting records shorter** (snippets of 200, 160 or 120 characters instead of 240): no more needles at 2,000 sessions (88, 88 and 75 of 120) and fewer at 200 sessions (118, 118 and 95 of 120 against 120).
* **Reading a third page in the hook**: the budget shows about 31 records whatever is fetched, so it would only cost time.
* **A larger candidate window**, not shipped: a record has to be read to be scored, the needles it would add at 6,000 sessions are the ones a narrower query finds anyway, and every other part of the contract (the 256, the trace field, the dense channel) is built around the window.

## Results

### Needles carried (development seeds 1 to 5, 120 needles at each size; the code was tuned on these)

| dev seeds 1 to 5 (tuned on): needles carried | 20 before | 20 after | 200 before | 200 after | 2,000 before | 2,000 after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prompt hook (step one: hook and start briefing) | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 51/120 (42%) | 83/120 (69%) |
| prompt hook alone | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 51/120 (42%) | 83/120 (69%) |
| `recall`, whole prompt as the query | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 66/120 (55%) | 76/120 (63%) |
| `recall`, careful query (subject and function names) | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 74/120 (62%) | 81/120 (68%) |
| two-step reader, after step one (hooks) | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 51/120 (42%) | 83/120 (69%) |
| two-step reader, after step two (hooks + `recall` from prompt and file) | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 96/120 (80%) | 106/120 (88%) |
| two-step reader, after step three (+ the next page) | n/a | 120/120 (100%) | n/a | 120/120 (100%) | n/a | 115/120 (96%) |

Characters the readers put in front of the model (mean per prompt):

| mean per prompt | 20 before | 20 after | 200 before | 200 after | 2,000 before | 2,000 after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| hook characters | 2,351 | 2,448 | 4,743 | 3,874 | 5,686 | 5,721 |
| `recall` characters (whole prompt) | 5,278 | 5,527 | 16,210 | 16,655 | 17,078 | 17,320 |

The two-step reader's second `recall` alone (query from the prompt and the file) carries 82 of 120 needles at 2,000 sessions before and 92 after. The prompt hook alone and the first step of the reader are the same figure, because the start briefing carries no needle at 200 sessions and above.

### Held-out seed 14 (scored once, after the code was frozen)

| held-out seed 14 (scored once): needles carried | 20 before | 20 after | 200 before | 200 after | 2,000 before | 2,000 after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prompt hook (step one: hook and start briefing) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 11/24 (46%) | 16/24 (67%) |
| prompt hook alone | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 11/24 (46%) | 16/24 (67%) |
| `recall`, whole prompt as the query | 24/24 (100%) | 24/24 (100%) | 23/24 (96%) | 23/24 (96%) | 13/24 (54%) | 18/24 (75%) |
| `recall`, careful query (subject and function names) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 14/24 (58%) | 19/24 (79%) |
| two-step reader, after step one (hooks) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 11/24 (46%) | 16/24 (67%) |
| two-step reader, after step two (hooks + `recall` from prompt and file) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 18/24 (75%) | 22/24 (92%) |
| two-step reader, after step three (+ the next page) | n/a | 24/24 (100%) | n/a | 24/24 (100%) | n/a | 23/24 (96%) |

Characters (mean per prompt):

| mean per prompt | 20 before | 20 after | 200 before | 200 after | 2,000 before | 2,000 after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| hook characters | 2,240 | 2,339 | 4,306 | 3,528 | 5,652 | 5,758 |
| `recall` characters (whole prompt) | 4,907 | 5,119 | 16,350 | 16,877 | 17,088 | 17,417 |

On the held-out seed the prompt hook and every `recall` carry more needles than before at 2,000 sessions (11 to 16, 13 to 18 and 14 to 19 of 24; the reader after step two 18 to 22, after step three 23), and 20 and 200 sessions stay at 100% except one needle that neither the original nor the final code puts in the plain `recall` at 200 sessions. Track T2 on the same seed (8 tasks, 2,000 sessions): the current value reaches the prompt hook for 8 of 8 tasks (5 before) and the `recall` for the task prompt for 8 of 8 (3 before). Eight tasks and 24 needles are small: one task is a quarter of a point of difference on a percentage.

### A corrected fact (track T2, 8 tasks on each of 5 development seeds, 2,000 sessions): is the current value shown?

| View | Current value shown, before | after | Current value first, before | after | Current value not shown, before | after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| prompt hook | 18 of 40 | 39 | 9 | 33 | 22 | 1 |
| `recall`, the task prompt | 24 | 38 | 16 | 31 | 16 | 2 |
| `recall`, the subject phrase | 16 | 28 | 11 | 25 | 24 | 12 |
| `recall`, subject and every attribute label (names the property; the labels are the generator's) | 16 | 28 | 13 | 25 | 24 | 12 |
| the hooks (start and prompt) | 18 | 39 | 8 | 33 | 22 | 1 |

"Current value first" means the current sentence stands before every earlier one in that text; "shown" also counts the cases where an earlier value is first. At 20 and 200 sessions the same views stayed as the P2-12 record has them (below).

### What it costs (2,000 sessions)

Tokens (the briefing's own estimate, three characters a token; the budget of the prompt hook is unchanged at 2,000 tokens and 1.5 seconds): the prompt hook shows 5,721 characters on average on the development seeds (5,686 before), about 1,907 tokens, and 31 records on average where it showed 23; a `recall` with the whole prompt returns 17,320 characters (17,078 before), about 5,770 tokens of its 6,000, for 32 records (31.9 on average, as before). At 200 sessions the hook is shorter (3,874 characters, 4,743 before: the Sources lines of a list cut short are no longer printed); at 20 sessions about the same (2,448, 2,351).

Time, three alternating runs (original code, final code, original, ...) of the 12 tasks of seed 1 at 2,000 sessions on an otherwise quiet host (`hookprobe`, on the tree before the last wording changes; the hook figure is the whole command, interpreter start-up included, one process per hook; 36 hook runs and 36 `recall` calls per code version):

| | Mean of the three runs (each run's mean) | Median task | Slowest task |
| --- | ---: | ---: | ---: |
| prompt hook, original | 0.389 s (0.364, 0.381, 0.421) | 0.382 s | 0.539 s |
| prompt hook, final | **0.539 s** (0.558, 0.538, 0.520) | 0.529 s | 0.611 s |
| `recall`, whole prompt, original | 0.090 s (0.087, 0.086, 0.096) | 0.084 s | 0.152 s |
| `recall`, whole prompt, final | 0.085 s (0.087, 0.085, 0.084) | 0.081 s | 0.146 s |
| `recall`, careful query, original | 0.061 s (0.059, 0.060, 0.065) | 0.057 s | 0.132 s |
| `recall`, careful query, final | 0.063 s (0.065, 0.062, 0.062) | 0.062 s | 0.101 s |

The hook is 0.15 s slower because it reads the second page when more than 32 matched (a second lookup after the first, 0.1 s) and the user's memory lookup runs beside them; the deadline is 1.5 s and the slowest hook of the 36 took 0.61 s. A `recall` costs the same as before.

### Nothing that P2-12 settled got worse (the pre-flight of tracks T2, T2f, T3 and T4, development seeds 1 to 3, 20 and 200 sessions, before and after)

`benchmarks/longhaul/preflight.py --track T2 T2f T3 T4 --size 20 200 --seeds 1 2 3`, the same command on the original code and on the final code. The two outputs are identical except for two lines, both track T4 at 200 sessions (below). So: at 20 and 200 sessions the prompt hook still puts the current value first in 24 of 24 tasks; a forgotten sentence is on no file after a forget (the receipt table and a query that quoted it keep it, as in the P2-12 record); a fact of one project is shown in no other project; the user-scope preference reaches the prompt hook as before; the canary of a leak task is in no view; and the hostile text of a tool result is in no start briefing or prompt hook.

The two lines that differ: the **assistant's own repetition of a hostile page** (the known weak point named in P2-12, shown as the assistant's words and labelled as evidence) is now shown by the prompt hook in 8 of 18 T4 tasks at 200 sessions where it was shown in 6, because the hook now fits more records of a history. It is the same framing as before, and the third-party text itself (what a tool or a page wrote) is still shown by nothing automatic. This is a cost of the change and is stated as such.

### The retrieval gates of `tools/check.py` (no threshold lowered)

| Gate | Before | After | Threshold |
| --- | ---: | ---: | ---: |
| LongMemEval regression sample, turn recall any at 5 / at 10 | 0.722 / 0.819 | 0.722 / 0.819 | 0.67 / 0.78 |
| turn recall all at 5 / at 10 | 0.542 / 0.694 | 0.542 / **0.681** | 0.52 / 0.67 |
| turn MRR | 0.5232 | 0.5231 | 0.50 |
| answer-turn recall at k | 0.819 | 0.819 | 0.78 |
| session recall any at 5 / at 10, all at 5 / at 10 | 0.847 / 0.903, 0.778 / 0.847 | 0.847 / 0.903, 0.778 / 0.861 | 0.81 / 0.86, 0.74 / 0.81 |
| session MRR (distinct) | 0.792 | 0.792 | 0.76 |
| retrieval benchmark: recall, precision, MRR, window recall, mean tokens | 1.0, 0.532, 1.0, 1.0, 3,853 | 1.0, 0.532, 1.0, 1.0, 3,858 | 0.95, 0.5, 0.9, 0.9, 6,000 |
| stale facts, current record first, overall | 0.400 | 0.467 | 0.33 |
| stale facts, current record first, accepted claims (`claim`) | 0.30 | 0.60 | no threshold |

The one number that moved toward its threshold is the sample's turn recall-all at 10, one question (0.0139) above it. The final constants of the spread were set so that this gate passes unchanged; they sit at the edge of what it allows.

### Larger than the benchmark: 6,000 sessions (one history, seed 1, 24 needles; not part of the tables above)

Seeded once at 6,000 sessions (about 40 MB) and read with the pool diagnostic, on the final code's pool (the window and the searches are the same as before the change):

| Query | Needles not in the candidate pool at 2,000 sessions | at 6,000 sessions | Carried at 6,000 |
| --- | ---: | ---: | ---: |
| prompt hook (its own query, floor and limit) | 0 of 24 | 6 of 24 | 10 of 24 (22 of 24 at 2,000) |
| careful query | 0 of 24 | 9 of 24 | 8 of 24 (16 at 2,000) |
| two-step query | 0 of 24 | 10 of 24 | 7 of 24 (19 at 2,000) |

The pool was full (256 scored candidates) for every one of the 24 queries of each kind at 6,000 sessions, against 6 to 14 of 24 at 2,000. A needle that is not in the pool cannot be ranked, shown or paged to; this is the limit of a window of the newest candidates, not tokenisation or the floor, and no ranking change reaches it. What does: naming the attribute. With the query the subject phrase and the attribute's own label (an agent that has read the code and knows what to ask: the generator's words, so an upper bound), `recall` carried 21 of 24 needles at 6,000 sessions with the plain query and 21 of 24 with `match: "all"`, against 8 of 24 for the careful one; at 2,000 sessions 24 of 24 and 24 of 24 against 16. The reply now says "at least N matched" and that an older record needs the narrowing, when the window was full.

## What stays weak, and whether a real agent should now do better

* **The hook still cannot know the attribute.** At 2,000 sessions it carries 69% of the needles on the development seeds and 67% on the held-out seed; the rest are among a hundred near-equal records about the same component, and the list shows 31 of them. A reader that follows the reply's own instructions does better: after one `recall` built from the prompt and the file the task names the scripted reader carries 88% (92% on seed 14), and 96% after reading on one page. But in the generated repositories the file names none of the attributes, so what a reader gains at step two is mostly the function's own words and the second look at the list; a real agent has to think of the attribute (the delimiter, the decimal places) and ask for it. The offline measure cannot say whether it does.
* **The effect on a real agent is unmeasured.** A scripted reader finds a sentence in a text; it does not decide, and "surfaced but not used" is a property of the reader. The change makes the near-identical neighbours fewer and the later-wins line more careful, and it says in the list, in the tool description and in the server's instructions what to ask next, but nothing here shows that an agent now reads those lines. A real agent at 2,000 sessions should find the needed record more often than the 5 of 24 the hook carried in the last real run (seed 13, not touched here; on the fresh seed 14 the offline hook figure moves from 11 to 16 of 24), and the needed record should be in the first page of a `recall` that names the attribute nearly every time (24 of 24 in the one 2,000-session history checked, 21 of 24 at 6,000). Whether it passes more tasks than the raw transcripts' 8 of 12 is a question for a real run; the honest expectation is "somewhat more", not "all".
* **The window.** A generic query in a history of 6,000 sessions compares only the newest 256 candidates (6 to 10 of 24 needles are not among them in the one history tried). The reply now says so ("at least N matched ... an older record needs the narrowing"), and the narrowing finds them, but an agent that does not narrow cannot see them. A schema-level remedy, not made here: index a record's passages (sentences) separately so a candidate is chosen by passage score instead of recency, which needs a new table and the migration chain on both backends.
* **The label** (what each record names) was rarely printed in these histories, because every record of a component names the same file; its value in a history where records name different files is not measured.
* **Constants at the edge of a gate.** The spread's band (0.7) and crowd size (16) were set so that the LongMemEval regression sample's gate passes unchanged; its turn recall-all at 10 is one question above its threshold. A wider spread carries more needles offline (88 of 120 in the hook against 83 for the shipped one) and fails that gate.
* **T4.** The assistant's own repetition of a hostile page is shown by the prompt hook in 8 of 18 tasks at 200 sessions (6 before): the hook fits more records. The third-party text itself is still shown by nothing automatic.
* **Everything here is a development seed set the changes were tuned on, a synthetic generator and a scripted reader.** The held-out seed is one seed (24 needles, 8 T2 tasks); its direction agrees with the development seeds, and its sizes are the ones a seed gives.
