"""The rule that recognises a standing personal preference in the user's own words
(`memoryking.domain.preference`): what counts, and, above all, what never does.

The sentences were written for these tests, not taken from the benchmark; a few vary the wording of
the benchmark generator's three preference templates. A missed preference costs a sentence the user
can say again; a recognised project fact is shown in another project, so every near miss of the
second kind is a test here."""

import json
import unittest
from pathlib import Path

from memoryking.domain.preference import (
    MAX_PER_CAPTURE,
    accepted_proposals,
    evidence,
    is_acceptance,
    is_preference,
    preferences,
)

RECOGNISED = [
    "I want dates written as ISO 8601 everywhere I work, in all my projects.",
    "Whatever the project, I like truncation toward zero as the rounding mode.",
    "Standing preference for all my projects: lower case for log levels.",
    "I want lower case for the level everywhere I work, in every project, not only here.",
    "In all my projects I want commit messages in the imperative mood.",
    "Across all my projects, I want error messages written in plain English.",
    "I'd like summaries kept to three lines, whatever the repo.",
    "Wherever I work, I want the date shown as day month year.",
    "My standing preference is Oxford commas, in all my projects.",
    "Regardless of the project, I expect you to explain a risky command before running it.",
    "I do not want emojis in anything you write for me, in any project I work on.",
    "No matter which repo, I hate long functions with deeply nested branches.",
    "I never want you to force-push for me, whatever the project.",
    "Never use emojis for me, in all my projects.",
    "I never want secrets echoed back to me, in any of my projects.",
    "For all my projects: I want a changelog line with every change.",
]
# A preference with no scope of its own may be a fact of one project: it stays there.
STAYS_IN_THE_PROJECT = [
    "I always want dates written as ISO 8601.",
    "I prefer British English spelling.",
    "I never want you to add comments that only restate the code.",
    "I generally prefer pnpm to npm.",
    "By default I want times in UTC.",
    "In every project, I want commit messages in the imperative mood.",
    "I want lower case for the level in every project.",
    "Always use pnpm.",
    "Never edit legacy/ in this repo.",
    "In this repo always use tabs.",
    "In the billing service I always want amounts in cents.",
    "I always want the importer to log at debug level.",
    "I want the retry set to 4 in this project.",
    "We decided to keep the retry at 4.",
    "This service uses port 8080.",
    "The API timeout is 30 seconds and I want it to stay that way.",
    "I want the export delimiter to be a semicolon.",
    "Decision for the payments service: the rounding is half-even. Use that from now on.",
    "Rule: the importer must always use UTF-8 as the encoding. No exceptions.",
    "Personally I want the rate limit of the gateway to be 100; keep that in mind.",
]
# Worded like a preference about the project, or tied to it, with a scope phrase in it.
PROJECT_FACTS_THAT_SOUND_GENERAL = [
    "In project alpha I always want the report grid to be weekly, whatever the other projects do.",
    "I need the build to pass on every project in this monorepo, in all my projects.",
    "I want every project in the workspace to have a Makefile.",
    "Whatever the problem, I want to see the logs first.",
    "I want the logger in all my modules to use the shared handler.",
    "I want you to read src/app.py and summarise it, in all my projects.",
    "For all my tests, use the fixture in conftest.py.",
    "I always want the importer to run with --strict in all my projects.",
    "I want `retry_limit` set to 3 in all my projects.",
    "I want https://example.com/api used in all my projects.",
    "I want the API on port 8080 in all my projects, for now.",
    "She prefers short names in all her projects.",
    "My manager wants the same layout in all my projects.",
    "Is it true that I always want ISO dates in all my projects?",
    "If I prefer dates as ISO 8601 in all my projects, set it up for this repo.",
    "I want the same date format as the other project uses, in all my projects.",
    "I need all my changes in this PR reviewed, in all my projects.",
    "We agreed that I want this in all my projects.",
    "I want the nightly job to be quiet in all my projects today.",
    "Decision: the API returns 404 for unknown ids, in all my projects.",
]


class RuleTests(unittest.TestCase):
    def test_a_stated_preference_that_names_every_project_is_recognised(self):
        for sentence in RECOGNISED:
            self.assertTrue(is_preference(sentence), sentence)

    def test_a_preference_that_names_no_scope_stays_in_the_project(self):
        for sentence in STAYS_IN_THE_PROJECT:
            self.assertFalse(is_preference(sentence), sentence)

    def test_a_project_fact_is_not_recognised_even_when_it_sounds_general(self):
        for sentence in PROJECT_FACTS_THAT_SOUND_GENERAL:
            self.assertFalse(is_preference(sentence), sentence)

    def test_a_secret_or_a_path_or_code_never_crosses(self):
        for tail in ("`rm -rf build`", "src/app.py", "api_key", "MY_TOKEN_NAME", "handleError()"):
            self.assertFalse(is_preference(f"In all my projects I want {tail} used."), tail)

    def test_length_bounds(self):
        long = "I want " + "very " * 80 + "short answers in all my projects."
        self.assertFalse(is_preference(long))

    def test_sentences_are_taken_from_a_turn_one_by_one(self):
        turn = (
            "User: Fix the report page, it came up in standup again. "
            "Whatever the project, I like dates as ISO 8601; please apply that wherever it fits. "
            "This service uses port 8080. In this repo always use tabs."
        )
        self.assertEqual(
            preferences(turn),
            ["Whatever the project, I like dates as ISO 8601; please apply that wherever it fits."],
        )

    def test_code_and_quoted_lines_are_not_read(self):
        turn = (
            "```\nWhatever the project, I like tabs everywhere I work.\n```\n\n"
            "> In all my projects I want commit messages in the imperative mood.\n"
            "| Whatever the project, I like dates as ISO 8601.\n"
        )
        self.assertEqual(preferences(turn), [])

    def test_text_a_tool_result_was_cut_out_of_is_not_read(self):
        # What the hooks keep of a turn that copied a tool result: a marker, not the copy.
        turn = (
            "User: Here is what the file says: [quoted text left out: 140 characters, kept in "
            "the tool-output record]"
        )
        self.assertEqual(preferences(turn), [])

    def test_one_capture_adds_a_few_sentences_at_most_and_each_once(self):
        many = " ".join(
            f"Whatever the project, I like {word} as the rounding mode."
            for word in "alpha beta gamma delta epsilon zeta eta".split()
        )
        self.assertEqual(len(preferences(many)), MAX_PER_CAPTURE)
        twice = "Whatever the project, I like dates as ISO 8601. " * 3
        self.assertEqual(len(preferences(twice)), 1)

    def test_speaker_labels_and_blank_lines(self):
        turns = "User: Thanks.\n\nUser: In all my projects I want commit messages in the imperative mood."
        self.assertEqual(
            preferences(turns),
            ["In all my projects I want commit messages in the imperative mood."],
        )


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "accepted_preference_pairs.json").read_text()
)


def captured(assistant: str, user: str) -> list[tuple[str, str]]:
    return accepted_proposals([f"Assistant: {assistant}", f"User: {user}"])


class AcceptedProposalTests(unittest.TestCase):
    """A preference the assistant proposes and the user's next turn accepts. The pairs are in
    fixtures/accepted_preference_pairs.json, written for these tests."""

    def test_written_acceptances_of_a_stated_preference_are_recognised(self):
        self.assertGreaterEqual(len(FIXTURE["positive"]), 20)
        for pair in FIXTURE["positive"]:
            got = captured(pair["assistant"], pair["user"])
            self.assertEqual(len(got), 1, pair)
            sentence, said = got[0]
            self.assertIn(sentence, " ".join(pair["assistant"].split()))
            self.assertEqual(said, pair["user"])

    def test_no_written_negative_pair_is_ever_recognised(self):
        self.assertGreaterEqual(len(FIXTURE["negative"]), 60)
        for pair in FIXTURE["negative"]:
            self.assertEqual(captured(pair["assistant"], pair["user"]), [], pair)

    def test_the_known_misses_stay_misses(self):
        # A scope phrase with nobody's preference named could be a report of a fact.
        for pair in FIXTURE["missed_by_design"]:
            self.assertEqual(captured(pair["assistant"], pair["user"]), [], pair)

    def test_the_stored_evidence_shows_both_sets_of_words(self):
        text = evidence("You prefer ISO dates in all your projects.", "Yes, exactly.")
        self.assertEqual(
            text,
            "The assistant proposed: You prefer ISO dates in all your projects.\n"
            "The user accepted: Yes, exactly.",
        )

    def test_only_two_adjacent_conversation_turns_make_a_pair(self):
        proposal = "Assistant: You prefer ISO dates in all your projects."
        yes = "User: Yes."
        self.assertEqual(len(accepted_proposals([proposal, yes])), 1)
        for pieces in (
            [yes, proposal],
            [proposal, "Assistant: Done.", yes],
            [proposal, "Tool error: boom", yes],
            [proposal, "User: Run the tests.", yes],
            [yes],
            [],
        ):
            self.assertEqual(accepted_proposals(pieces), [], pieces)

    def test_the_users_own_turn_is_never_the_proposal(self):
        self.assertEqual(
            accepted_proposals(["User: You prefer ISO dates in all your projects.", "User: Yes."]),
            [],
        )
        self.assertEqual(
            accepted_proposals(
                ["Assistant: Ok.", "User: I prefer ISO dates in all my projects.", "User: Yes."]
            ),
            [],
        )

    def test_a_cut_out_copy_of_a_tool_result_makes_the_turn_ineligible(self):
        turn = (
            "You prefer ISO dates in all your projects. [quoted text left out: 140 characters, "
            "kept in the tool-output record]"
        )
        self.assertEqual(captured(turn, "Yes."), [])

    def test_one_capture_keeps_a_few_pairs_and_each_sentence_once(self):
        pieces = []
        for word in "alpha beta gamma delta epsilon zeta eta".split():
            pieces += [
                f"Assistant: You prefer {word} as the rounding mode in all your projects.",
                "User: Yes.",
            ]
        self.assertEqual(len(accepted_proposals(pieces)), MAX_PER_CAPTURE)
        same = ["Assistant: You prefer ISO dates in all your projects.", "User: Yes."] * 3
        self.assertEqual(len(accepted_proposals(same)), 1)

    def test_a_long_reply_is_not_an_acceptance(self):
        self.assertTrue(is_acceptance("Yes, that is settled. Thanks."))
        self.assertFalse(is_acceptance("Yes, " + "really " * 12 + "thanks."))
        self.assertFalse(is_acceptance("thanks"))
        self.assertFalse(is_acceptance(""))


if __name__ == "__main__":
    unittest.main()
