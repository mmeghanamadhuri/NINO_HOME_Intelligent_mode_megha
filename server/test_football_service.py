"""Regression tests for live football-score lookups."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from football_service import (
    FifaTournamentService,
    FootballNotConfiguredError,
    FootballService,
    live_football_voice_reply,
)
from voice_service import (
    fifa_world_cup_top_scorer_year,
    fifa_world_cup_winner_year,
    is_football_joke_request,
    is_football_question,
    is_live_football_question,
    is_world_cup_favourite_question,
    world_cup_favourite_reply,
)


LIVE_FIXTURE = {
    "fixture": {"status": {"short": "2H", "elapsed": 74}},
    "league": {"name": "FIFA Club World Cup"},
    "teams": {"home": {"name": "Manchester City"}, "away": {"name": "Juventus"}},
    "goals": {"home": 2, "away": 1},
}


class FootballServiceTests(unittest.TestCase):
    def test_key_is_required(self) -> None:
        with self.assertRaises(FootballNotConfiguredError):
            FootballService(api_key="").live_matches()

    @patch("football_service.requests.get")
    def test_live_matches_are_normalized_and_cached(self, get: MagicMock) -> None:
        response = MagicMock()
        response.json.return_value = {
            "response": [LIVE_FIXTURE, {"fixture": {"status": {"short": "FT"}}}]
        }
        get.return_value = response
        service = FootballService(api_key="test-key", cache_ttl_seconds=60)

        first = service.live_matches()
        second = service.live_matches()

        self.assertEqual(first, [LIVE_FIXTURE])
        self.assertEqual(second, [LIVE_FIXTURE])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(
            live_football_voice_reply(first),
            "Live football update: Manchester City 2, Juventus 1, "
            "in the 74th minute in FIFA Club World Cup.",
        )

    @patch("football_service.requests.get")
    def test_world_cup_winner_is_read_and_cached(self, get: MagicMock) -> None:
        response = MagicMock()
        response.json.return_value = {
            "query": {
                "pages": {
                    "1": {
                        "extract": (
                            "The 2026 FIFA World Cup concluded on July 19 "
                            "with Spain winning the championship for the second time."
                        )
                    }
                }
            }
        }
        get.return_value = response
        service = FifaTournamentService(cache_ttl_seconds=300)

        self.assertEqual(service.world_cup_winner(2026), "Spain")
        self.assertEqual(service.world_cup_winner(2026), "Spain")
        self.assertEqual(get.call_count, 1)

    @patch("football_service.requests.get")
    def test_world_cup_top_scorer_is_read_and_cached(self, get: MagicMock) -> None:
        sections_response = MagicMock()
        sections_response.json.return_value = {
            "parse": {"sections": [{"line": "Goalscorers", "index": "46"}]}
        }
        goals_response = MagicMock()
        goals_response.json.return_value = {
            "parse": {
                "text": {
                    "*": (
                        '<p><b>10 goals</b></p><ul><li><a '
                        'title="France national football team">France</a> '
                        '<a title="Kylian Mbappé">Kylian Mbappé</a></li></ul>'
                        "<p><b>8 goals</b></p>"
                    )
                }
            }
        }
        get.side_effect = [sections_response, goals_response]
        service = FifaTournamentService(cache_ttl_seconds=300)

        self.assertEqual(service.world_cup_top_scorer(2026), ("Kylian Mbappé", 10))
        self.assertEqual(service.world_cup_top_scorer(2026), ("Kylian Mbappé", 10))
        self.assertEqual(get.call_count, 2)


class FootballVoiceRoutingTests(unittest.TestCase):
    def test_football_questions_are_detected(self) -> None:
        for text in (
            "What are the live football scores?",
            "Give me a FIFA update",
            "Is there a Premier League match now?",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_football_question(text))

    def test_non_football_questions_are_not_detected(self) -> None:
        self.assertFalse(is_football_question("What is the weather today?"))

    def test_live_football_questions_require_live_context(self) -> None:
        self.assertTrue(is_live_football_question("What live football match is on now?"))
        self.assertFalse(
            is_live_football_question("Who scored the highest points in FIFA 2026?")
        )

    def test_football_joke_request_is_detected(self) -> None:
        self.assertTrue(is_football_joke_request("Tell me a football joke."))
        self.assertTrue(is_football_joke_request("Can you share a soccer joke?"))
        self.assertFalse(is_football_joke_request("Tell me a joke about weather."))

    def test_world_cup_favourite_question_is_detected(self) -> None:
        self.assertTrue(
            is_world_cup_favourite_question(
                "Since the World Cup is on, who's your favourite?"
            )
        )
        self.assertTrue(
            is_world_cup_favourite_question("Who are you rooting for at the FIFA World Cup?")
        )
        self.assertFalse(is_world_cup_favourite_question("Who won the World Cup?"))

    def test_world_cup_favourite_reply_does_not_request_live_scores(self) -> None:
        self.assertEqual(world_cup_favourite_reply(), "My favourite is Brazil.")

    def test_world_cup_winner_year_is_detected(self) -> None:
        self.assertEqual(fifa_world_cup_winner_year("Who won FIFA 2026?"), 2026)
        self.assertEqual(
            fifa_world_cup_winner_year("Who was the 2022 World Cup champion?"), 2022
        )
        self.assertIsNone(fifa_world_cup_winner_year("Give me a FIFA update"))

    def test_world_cup_top_scorer_year_is_detected(self) -> None:
        self.assertEqual(
            fifa_world_cup_top_scorer_year("Who scored the highest points in FIFA 2026?"),
            2026,
        )
        self.assertEqual(
            fifa_world_cup_top_scorer_year("Who won the Golden Boot in World Cup 2022?"),
            2022,
        )
        self.assertIsNone(fifa_world_cup_top_scorer_year("Who won FIFA 2026?"))


if __name__ == "__main__":
    unittest.main()
