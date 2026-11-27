"""Reading the turns beside a result, on both backends: never wider than a search may read."""

import json
import unittest

import test_knowledge as knowledge
import test_postgres_parity as pg

from memoryking.adapters.episodes import GAP_MS, Episodes
from memoryking.adapters.retrieval import Retrieval
from memoryking.adapters.trust import Trust

MINUTE = 60_000


class EpisodeCases:
    clock = 0

    def turn(self, key, project="q", source="chat", readers=None, at=None, text=None):
        key = "t-" + key
        self.clock += MINUTE
        return self.store.capture(
            self.owner,
            self.ns,
            key,
            {
                "version": 1,
                "project": project,
                "source": source,
                "source_event": key,
                "text": text or f"turn {key}",
                "readers": readers,
                "classification": "internal",
                "observed_at_ms": at if at is not None else self.clock,
            },
        )["observation"]

    def episodes(self, recorded=10**9):
        who = self.store._auth(self.owner, "read", "q", 1)
        return Episodes(self.store, who, recorded, 1)

    def episode(self, episodes, observation):
        row = self.store.observation_rows("main", [observation])[observation]
        return [member["id"] for member in episodes.of(row)]

    def ids(self, episodes, observation, reach):
        row = self.store.observation_rows("main", [observation])[observation]
        return [member["id"] for member in episodes.beside(row, reach)]

    def test_the_turns_around_a_hit_are_its_episode_nearest_first(self):
        a, b, c, d, e = (self.turn(k) for k in "abcde")
        found = self.ids(self.episodes(), c, 2)
        self.assertEqual([d, b, e, a], found)
        self.assertEqual([a, b, c, d, e], self.episode(self.episodes(), c))
        self.assertEqual([], self.ids(self.episodes(), c, 0))

    def test_a_long_silence_ends_the_episode(self):
        a = self.turn("a")
        b = self.turn("b", at=self.clock + GAP_MS + 1)
        c = self.turn("c", at=self.clock + 1)
        self.assertEqual([b, c], self.episode(self.episodes(), b))
        self.assertEqual([a], self.episode(self.episodes(), a))

    def test_another_source_or_project_is_never_part_of_the_run(self):
        a = self.turn("a")
        self.turn("tool", source="chat-tool")
        self.turn("other", project="elsewhere")
        b = self.turn("b")
        self.assertEqual([a, b], self.episode(self.episodes(), a))

    def test_a_held_purged_or_restricted_record_is_skipped_and_never_shown(self):
        a, held, c = self.turn("a"), self.turn("held"), self.turn("c")
        gone, hidden, f = (
            self.turn("gone"),
            self.turn("hidden", readers=["someone_else"]),
            self.turn("f"),
        )
        Trust(self.store).hold("main", "q", held, "chat", 1)
        self.store.purge(self.owner, gone)
        for start in (a, c, f):
            self.assertEqual([a, c, f], self.episode(self.episodes(), start))
            seen = self.ids(self.episodes(), start, 5)
            self.assertFalse({held, gone, hidden} & set(seen))

    def test_a_record_captured_after_the_briefing_position_is_not_read(self):
        a, b = self.turn("a"), self.turn("b")
        row = self.store.observation_rows("main", [b])[b]
        later = self.turn("later")
        episodes = self.episodes(recorded=row["position"])
        self.assertEqual([a, b], self.episode(episodes, b))
        self.assertNotIn(later, self.episode(episodes, a))


class BriefingCases(EpisodeCases):
    """A text briefing fills what is left of its budget with the turns beside what it shows."""

    def talk(self, **kinds):
        """Four turns, the second holds the query words; `kinds` changes the neighbour after it."""
        ids = {}
        for key, text in (
            ("before", "we spoke about the weekend before that"),
            ("hit", "the zukatori dentist visit was moved"),
            ("after", "to thursday at nine in the morning"),
            ("later", "and then lunch afterwards"),
        ):
            self.clock += MINUTE
            ids[key] = self.turn(key, text=text, readers=kinds.get(key))
        return ids

    def brief(self, query, **extra):
        output = []
        Retrieval(self.store).execute(
            self.owner,
            {
                "version": 1,
                "incarnation": self.store.incarnation,
                "action": "brief",
                "args": {
                    "project": "q",
                    "query": query,
                    "bindings": [{}],
                    "max_output_tokens": 6000,
                    "tokenizer": "utf8-byte-estimate-v1",
                    **extra,
                },
            },
        ).emit(output.append)
        return json.loads(output[0])

    def shown(self, **extra):
        packet = self.brief("zukatori dentist", format="text", **extra)
        return packet, {source["id"] for source in packet["sources"]}

    def test_the_turns_beside_a_hit_fill_the_briefing(self):
        ids = self.talk()
        packet, found = self.shown()
        self.assertEqual({ids["before"], ids["hit"], ids["after"], ids["later"]}, found)
        self.assertIn("thursday at nine", packet["text"])
        for line in packet["text"].splitlines():
            self.assertNotIn("neighbouring_turn", line)

    def test_a_structured_briefing_and_an_exact_search_add_no_neighbours(self):
        ids = self.talk()
        refs = json.dumps(self.brief("zukatori dentist"))
        self.assertNotIn(ids["after"], refs)
        _, found = self.shown(match="all")
        self.assertEqual({ids["hit"]}, found)

    def test_a_neighbour_the_reader_may_not_read_is_never_brought_in(self):
        ids = self.talk(after=["someone_else"])
        _, found = self.shown()
        self.assertIn(ids["hit"], found)
        self.assertNotIn(ids["after"], found)
        packet, _ = self.shown()
        self.assertNotIn("thursday at nine", packet["text"])

    def test_a_held_or_purged_neighbour_is_never_brought_in(self):
        ids = self.talk()
        Trust(self.store).hold("main", "q", ids["after"], "test", 1)
        self.store.purge(self.owner, ids["before"])
        packet, found = self.shown()
        self.assertEqual({ids["hit"], ids["later"]}, found)
        self.assertNotIn("thursday at nine", packet["text"])
        self.assertNotIn("the weekend before", packet["text"])

    def test_a_neighbour_from_another_source_or_project_is_never_brought_in(self):
        ids = self.talk()
        self.clock += MINUTE
        self.turn("tool", source="chat-tool")
        self.turn("elsewhere", project="elsewhere")
        _, found = self.shown()
        self.assertEqual(set(ids.values()), found)

    def test_neighbours_never_take_a_ranked_place_or_exceed_the_budget(self):
        ids = self.talk()
        for budget in (450, 600, 900):
            packet, found = self.shown(max_output_tokens=budget)
            self.assertLessEqual(packet["usage"]["output_tokens"], budget, budget)
            if found:
                self.assertIn(ids["hit"], found, budget)

    def test_a_ranked_result_that_does_not_fit_is_not_replaced_by_a_neighbour_and_matches_are_counted_once(
        self,
    ):
        for n in range(10):
            self.turn(f"n{n}", text=f"zukatori dentist note {n} " + "detail " * 60)
        for budget in (700, 1200, 2000):
            packet = self.brief("zukatori dentist", format="text", max_output_tokens=budget)
            self.assertLessEqual(packet["usage"]["output_tokens"], budget)
            shown = len(packet["sources"])
            self.assertIn(f"Showing {shown} of 10 matches", packet["text"], budget)


class LocalEpisodeTests(EpisodeCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class LocalBriefingTests(BriefingCases, unittest.TestCase):
    setUp = knowledge.KnowledgeTests.setUp
    tearDown = knowledge.KnowledgeTests.tearDown


class PostgresEpisodeTests(EpisodeCases, pg.NativeFixture):
    pass


class PostgresBriefingTests(BriefingCases, pg.NativeFixture):
    pass


if __name__ == "__main__":
    unittest.main()
