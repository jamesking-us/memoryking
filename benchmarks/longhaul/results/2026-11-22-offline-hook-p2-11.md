# What the MemoryKing hooks show an agent, before and after roadmap item P2-11 (offline, scripted reader, no agent)

**What this is.** A scripted measurement of which needle sentences of track T1 reach the model's context through MemoryKing's hooks and `recall`, on seeded histories, with no agent and no model call of any kind. It says what was put in front of a model, not whether a real agent would have used it, and it is not evidence of real-task quality or of any improvement in an agent's pass rate (the real-agent record of the same hooks, before the change, is [2026-11-22-s0-s1-a7c08d0.md](2026-11-22-s0-s1-a7c08d0.md); nothing here replaces it). Histories are generated and seeded exactly as the benchmark does (`tools/longhaul_benchmark.py seed`, through the hook path, no model); the measure is `tools/longhaul_benchmark.py hookprobe` (`benchmarks/longhaul/hookprobe.py`).

**How it is scored.** For each of the 12 tasks of T1 at a size (2 needles each, so 24 needles; sizes 20, 200 and 2,000 sessions) it runs, against a copy of the seeded store and as that task's agent credential: (a) the installed `UserPromptSubmit` hook command with the real task prompt and the harness's scope sentence in front; (b) the installed `SessionStart` command; (c) one `recall` with that prompt as the query (the lean tool's defaults: 6,000 tokens, no limit); (d) one `recall` with the query a careful agent would write, built by a fixed rule from the prompt alone (the phrase after "settled for", plus the words of the function names the prompt gives in backticks; it never reads the history). A needle is *carried* when its own sentence (found in the generated history: in a needle session, naming the task's subject and the slot's attribute and stating the slot's current value; never a draft, a "tried" or a "TODO") is in the text, compared on letters and digits only. Hook wall time is the whole command (interpreter start-up included), one process per hook.

**Data discipline.** Tuned only on development seeds 1 to 5 (sizes 20, 200, 2,000; 120 needles per size). Seed 11, the measured seed, was seen (its failures were read) and not tuned on. Seed 12 is held out: seeded before the change was finished, scored once, for the original code and for the final code, after the code was frozen. Two things were fixed before that: the tuning loop found a 1,200-token default too small (below), and the first version of the hook lost needles at 20 sessions (a regression guard on the dev seeds, fixed).

**Before** is the commit this work started from (`bcded07`): the prompt hook shows at most 5 records in 1,200 tokens, whole. **After** is the change: the lookup returns the closest 32, the hook fills a 2,000-token budget, cuts records to their particular sentences when they do not all fit whole, shows a session once, says how many more matched and names `recall`; two scoring changes (a query word that nothing in the project holds no longer lowers every score; anchor overlap combines with the word score instead of replacing it, and ties on it go by word score).

## Needles carried (of 24 per seed and size; 120 on the five dev seeds)

| Seeds | Source | 20 before | 20 after | 200 before | 200 after | 2,000 before | 2,000 after |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| dev seeds 1 to 5 (tuned on) | (a) prompt hook | 107/120 (89%) | 120/120 (100%) | 17/120 (14%) | 120/120 (100%) | 0/120 (0%) | 49/120 (41%) |
| dev seeds 1 to 5 (tuned on) | (b) start briefing | 23/120 (19%) | 23/120 (19%) | 0/120 (0%) | 0/120 (0%) | 0/120 (0%) | 0/120 (0%) |
| dev seeds 1 to 5 (tuned on) | (c) recall, prompt as query | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 33/120 (28%) | 66/120 (55%) |
| dev seeds 1 to 5 (tuned on) | (d) recall, careful query | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 120/120 (100%) | 74/120 (62%) | 74/120 (62%) |
| seed 11 (seen, not tuned on) | (a) prompt hook | 22/24 (92%) | 24/24 (100%) | 3/24 (12%) | 24/24 (100%) | 0/24 (0%) | 13/24 (54%) |
| seed 11 (seen, not tuned on) | (b) start briefing | 4/24 (17%) | 4/24 (17%) | 0/24 (0%) | 0/24 (0%) | 0/24 (0%) | 0/24 (0%) |
| seed 11 (seen, not tuned on) | (c) recall, prompt as query | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 6/24 (25%) | 15/24 (62%) |
| seed 11 (seen, not tuned on) | (d) recall, careful query | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 19/24 (79%) | 19/24 (79%) |
| seed 12 (held out, scored once) | (a) prompt hook | 20/24 (83%) | 24/24 (100%) | 2/24 (8%) | 24/24 (100%) | 0/24 (0%) | 10/24 (42%) |
| seed 12 (held out, scored once) | (b) start briefing | 6/24 (25%) | 6/24 (25%) | 0/24 (0%) | 0/24 (0%) | 0/24 (0%) | 0/24 (0%) |
| seed 12 (held out, scored once) | (c) recall, prompt as query | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 5/24 (21%) | 12/24 (50%) |
| seed 12 (held out, scored once) | (d) recall, careful query | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 24/24 (100%) | 12/24 (50%) | 12/24 (50%) |

The measured real-agent seed 11 gave the original hook 20, 2 and 1 of 24 at the three sizes; this scripted run gives 22, 3 and 0 on the same seed, so the offline measure reproduces what the real runs showed.

## What reached the context (mean characters per run, and mean wall time of the command)

| Seeds | Source | 20 before | 20 after | 200 before | 200 after | 2,000 before | 2,000 after |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| dev seeds 1 to 5 | (a) prompt hook | 2,024 chars, 0.26 s | 2,299 chars, 0.25 s | 2,505 chars, 0.27 s | 4,810 chars, 0.30 s | 2,501 chars, 0.31 s | 5,677 chars, 0.41 s |
| dev seeds 1 to 5 | (b) start briefing | 2,814 chars, 0.25 s | 2,990 chars, 0.23 s | 2,652 chars, 0.25 s | 2,828 chars, 0.23 s | 2,675 chars, 0.26 s | 2,851 chars, 0.23 s |
| dev seeds 1 to 5 | (c) recall, prompt as query | 5,270 chars, 0.03 s | 5,270 chars, 0.02 s | 16,401 chars, 0.04 s | 16,406 chars, 0.04 s | 16,701 chars, 0.10 s | 17,269 chars, 0.10 s |

Hook time at 2,000 sessions, seed 11, three alternating runs of the 12 tasks on an otherwise quiet host (36 hook runs each): mean 0.275 s before and 0.376 s after (median 0.274 and 0.371, slowest 0.368 and 0.478). The hard deadline is 1.5 s; the host's own timeout for the hook is 5 s. The lookup is the smaller part (about 0.1 s); the rest is starting the interpreter.

Tokens (the briefing's own estimate, three characters a token): the 2,000-token budget is an upper bound. The hook now uses about 1,900 tokens at 2,000 sessions (5,677 characters) where it used about 830 before (2,501), about 1,600 at 200 sessions (4,810; before 2,505) and about 770 at 20 sessions (2,299; before 2,024), where the whole of what matched is shown.

## The budget, by data

Dev seeds 1 to 5, prompt hook (a), needles carried:

| Budget (tokens) | 20 | 200 | 2,000 | Characters shown at 2,000 |
| --- | ---: | ---: | ---: | ---: |
| 1,200 | 100% | 82% | 20% | 3,262 |
| 2,000 (the default) | 100% | 100% | 41% | 5,677 |
| 3,000 | not run | 100% | 55% | 7,253 |

Seed 11 at 1,200: 92% at 200 and 25% at 2,000. A budget of 3,000 shows every record the lookup returns (the contract caps it at 32), which is where (c) stops: 55%. The default is 2,000 because it is where 200 sessions are fully carried; 3,000 buys 14 points at 2,000 sessions for 1,500 more characters on every prompt that matches. A history of 2,000 sessions has more same-subject records than any budget shows.

## What the mechanism was (read from the 12 tasks of dev seed 1 at 2,000 sessions, the ranked lists of dev seeds 2 and 3 at 200 and 2,000, and the hook's own text for failing cases)

1. **The prompt names a file, and every record that mentions the file tied.** A prompt that says `in ledger/exporter.py` has an anchor (a path). The scorer gave every record that holds an anchor the share of the query's anchors it holds, as the score, replacing its word score when that was larger. With one anchor that is 1,000,000 for every session that opened the file, with two 500,000 (and 333,333 and 250,000 for three and four), and the many records tied at one score were ordered newest first: at 2,000 sessions 70 to 130 records tied (seed 1, all 12 tasks), and the sentence that stated the fact sat anywhere among them (rank 18 to 104 of the scored list). It was not the component's name, the kind of statement or duplicate captures that outranked the needle: the same-component records tied with it on the file, and the needle's own words (a decision, "settled") were what the tie threw away. The scoring change moves the needle's rank in the scored list (before folding repeats; seeds 2 and 3, 48 needles per size) from a median of 12 (range 2 to 22) to 5 (1 to 19) at 200 sessions, and from 40 (11 to 103) to 37 (1 to 85) at 2,000. So the ranking fix settles 200 sessions and only helps at 2,000, where the rest of the gain is showing more.
2. **Boilerplate lowered every score under the floor.** The harness prepends one sentence (`Work only inside the current directory ...`). Query words that no record holds counted in the highest possible score, so every real match scored lower and fell under the 300,000 floor (the probe finding in the record). Words nothing in the project holds are now left out of that total. On the 72-question LongMemEval sample the lookup still injects for 59 of 72 questions (58 before) and for none of 10 generic prompts.
3. **Five slots, and each one was a full transcript.** The hook showed 5 records of about 370 characters with their ids and dates, in 1,200 tokens. The remaining distinction is that at 200 sessions a project has 20 to 30 records about the same component and, at 2,000, 100 to 130; what separates the needle from them is only the attribute (the column delimiter, the decimal places), which the task prompt does not name, so no ranking from the prompt alone puts it first. What can be done is to show more of them, cheaper (22 records in 2,000 tokens at 2,000 sessions against 5) and to say that the list is partial.
4. Checked and not found to matter: project scope (the lookup is already restricted to the task's project, so a sibling project's statement cannot outrank), a user statement against an assistant statement (13 of 36 against 15 of 36 needles carried at 2,000 sessions, seeds 1 to 3), a later correction (T1 has none). One more cost showed on a crowded test history, not in the T1 outputs: a record that stood for near-identical ones carried a list of up to 16 of their ids in the hook's text, about 600 characters of a 3,600-character budget; the hook no longer prints those lists (the receipt still names every record).

## What was tried and dropped

* A bonus for query words that sit next to each other in a record ("invoice amounts" as a phrase): no gain in the needle's rank (median 5 to 5.5 at 200 sessions and 37 to 37 at 2,000, seeds 2 and 3), so not shipped.
* Showing records whole whenever eight of them fit: a 2,000-token budget then carried no more than 1,200 (83% against 85% at 200 sessions, 3 seeds), because eight whole records use the budget that twenty cut ones would; records are shown whole only when all of them fit.
* Cutting every record to its best-matching sentence by the prompt's words alone: it kept the closing line of a session that mentioned the same file, and lost needles at 20 sessions (100% to 75%); the repeated-sentence removal and whole records for small histories fixed that (back to 100%).
* Two other ways to stop the anchor tie: an anchor weight of 0.5 in the combination (a record naming every anchor then stopped scoring 1,000,000, which a test pins) and keeping the larger of the two scores but ordering ties by word score alone (92% at 20 sessions on seeds 1 to 3, because a prompt that names a file no longer lifted its records over the floor); shipped is the combination plus the tie order.
* A lower floor to recover needles: on the LongMemEval sample at a floor of 200,000 the lookup injected for 69 of 72 questions and for 6 of 10 generic prompts; the floor stays 300,000.

## What stays weak

* At 2,000 sessions the prompt hook carries 41% (5 dev seeds), 54% (seed 11) and 42% (seed 12) of the needles, and one `recall` for the whole prompt 55%, 62% and 50%. A project that size has about 100 records about the same component and the prompt does not say which attribute it needs. The honest part of the change (the text says the list is partial, how many more matched, and to call `recall` with the thing and the property) is the main remedy, and whether an agent does that is not measured here.
* The careful query (d) carries 62% (dev), 79% and 50% at 2,000 sessions, unchanged by this work: it holds only the subject and the function names, so it too finds the whole component's records, not the needed attribute; `recall` with the attribute named finds the needle near the top (checked by hand, not scored).
* The start briefing (b) is unchanged: it shows the newest items, which at 200 sessions and above never held a needle, and the store records no kind of statement it could prefer without reading the text, which its design rules out (selection by the source label only). The start note now says it lists only the newest items and names `recall`.
* Everything here is a scripted reader on a synthetic generator; the measured effect on a real agent is unknown. The pilot without the prompt hook passed 4 of 6 at 200 sessions because agents called `recall` themselves; whether the hook should be on by default is argued in the roadmap item.

## Gates

`tools/check.py` retrieval gates pass with no threshold lowered. Moved by the scoring changes (before, after): LongMemEval regression sample turn recall at 5 0.708 to 0.722, turn MRR 0.5206 to 0.5232, multi-session turn recall at 5 0.750 to 0.833; session-level metrics unchanged; the retrieval benchmark's numbers unchanged; the stale-facts benchmark's claim current-first rate 0.35 to 0.55 (overall 0.411 to 0.456), with three failing cases where the current record ranks third instead of second.
