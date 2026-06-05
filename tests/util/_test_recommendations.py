import os
import unittest
from unittest import mock

from lutris import settings
from lutris.database import categories as categories_db
from lutris.database import games as games_db
from lutris.database import schema
from lutris.util import recommendations
from lutris.util.test_config import setup_test_environment

setup_test_environment()


class RecommendationTester(unittest.TestCase):
    def setUp(self):
        if os.path.exists(settings.DB_PATH):
            os.remove(settings.DB_PATH)
        schema.syncdb()

    def add_game(self, name, **kwargs):
        game_id = games_db.add_game(name=name, slug=name.lower().replace(" ", "-"), **kwargs)
        return games_db.get_game_by_field(game_id, "id")

    def add_category(self, game_id, name):
        category = categories_db.get_category_by_name(name)
        category_id = category["id"] if category else categories_db.add_category(name, no_signal=True)
        categories_db.add_game_to_category(game_id, category_id, no_signal=True)

    def test_played_categories_raise_recommendations(self):
        played = self.add_game("Played RPG", playtime=20)
        candidate = self.add_game("Candidate RPG", playtime=0)
        unrelated = self.add_game("Unrelated", playtime=0)
        self.add_category(played["id"], "RPG")
        self.add_category(candidate["id"], "RPG")
        self.add_category(unrelated["id"], "Puzzle")

        ranked, reasons = recommendations.rank_locally([unrelated, candidate, played])

        self.assertEqual(ranked[0]["name"], "Candidate RPG")
        self.assertIn("Matches categories you play often", reasons[candidate["id"]].reasons)

    def test_platform_and_runner_do_not_affect_score(self):
        first = self.add_game("Same A", playtime=0, runner="wine", platform="Windows")
        second = self.add_game("Same B", playtime=0, runner="linux", platform="Linux")
        ranked_before, reasons_before = recommendations.rank_locally([second, first])

        first["runner"] = "linux"
        first["platform"] = "Linux"
        second["runner"] = "wine"
        second["platform"] = "Windows"
        ranked_after, reasons_after = recommendations.rank_locally([second, first])

        self.assertEqual([game["id"] for game in ranked_before], [game["id"] for game in ranked_after])
        self.assertEqual(reasons_before[first["id"]].score, reasons_after[first["id"]].score)
        self.assertEqual(reasons_before[second["id"]].score, reasons_after[second["id"]].score)

    def test_steam_rating_bonus_only_when_present(self):
        positive = self.add_game("Positive", service="steam", service_id="123")
        positive["steam_rating"] = "Very Positive"
        unrated = self.add_game("Unrated")
        unrated["steam_rating"] = "Very Positive"

        ranked, reasons = recommendations.rank_locally([unrated, positive])

        self.assertEqual(ranked[0]["id"], positive["id"])
        self.assertIn("Steam rating: Very Positive", reasons[positive["id"]].reasons)
        self.assertEqual(reasons[unrated["id"]].score, 0)

    def test_steam_service_id_counts_as_appid_for_cached_rating(self):
        game = self.add_game("Steam Game", service="steam", service_id="123")

        with mock.patch("lutris.util.steam_reviews._load_cache", return_value={"123": {"rating": "Very Positive", "updated_at": 9999999999}}):
            ranked, reasons = recommendations.rank_locally([game])

        self.assertEqual(ranked[0]["steam_review_summary"], "Very Positive")
        self.assertEqual(reasons[game["id"]].score, 2.0)
        self.assertIn("Steam rating: Very Positive", reasons[game["id"]].reasons)

    def test_unknown_llm_ids_are_discarded(self):
        first = self.add_game("First")
        second = self.add_game("Second")

        with mock.patch("lutris.util.recommendations._reorder_with_gemini", return_value=["unknown", second["id"]]):
            ranked, _reasons = recommendations.rank_recommended([first, second])

        self.assertEqual([game["id"] for game in ranked], [second["id"], first["id"]])

    def test_prompt_payload_excludes_private_and_runtime_fields(self):
        game = self.add_game(
            "Private",
            runner="wine",
            platform="Windows",
            directory="/home/user/private",
            executable="/home/user/private/game.exe",
        )
        self.add_category(game["id"], "RPG")

        payload = recommendations._minimal_game_payload(game, categories_db.get_categories_in_games([game["id"]]))

        self.assertEqual(
            set(payload),
            {"id", "name", "categories", "playtime", "favorite", "installed", "steam_rating", "year"},
        )
        self.assertNotIn("runner", payload)
        self.assertNotIn("platform", payload)
        self.assertNotIn("directory", payload)
