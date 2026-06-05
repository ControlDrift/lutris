"""Game recommendation ranking with an optional LLM reordering pass."""

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

import requests

from lutris.database import categories as categories_db
from lutris.util.llm_auth import DEFAULT_LLM_PROVIDER
from lutris.util.log import logger
from lutris.util.steam_reviews import apply_cached_steam_review_summaries, get_steam_appid
from lutris.util.strings import get_natural_sort_key

MAX_LLM_CANDIDATES = 50
GEMINI_GENERATE_CONTENT_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"


@dataclass(frozen=True)
class Recommendation:
    game_id: str
    score: float
    reasons: tuple[str, ...]


def _game_id(game: dict[str, Any]) -> str:
    return str(game.get("id") or game.get("appid") or game.get("slug") or "")


def _is_favorite(game_id: str, category_names: dict[str, list[str]]) -> bool:
    return "favorite" in category_names.get(game_id, [])


def _steam_rating_bonus(game: dict[str, Any]) -> tuple[float, str | None]:
    if not get_steam_appid(game):
        return 0.0, None
    rating_text = str(game.get("steam_rating") or game.get("steam_review_summary") or "")
    rating = rating_text.lower()
    if "overwhelmingly positive" in rating:
        return 3.0, f"Steam rating: {rating_text}"
    if "very positive" in rating:
        return 2.0, f"Steam rating: {rating_text}"
    if "positive" in rating:
        return 1.0, f"Steam rating: {rating_text}"
    if rating_text:
        return 0.0, f"Steam rating: {rating_text}"
    return 0.0, None


def _build_category_weights(games: list[dict[str, Any]], category_names: dict[str, list[str]]) -> Counter[str]:
    weights: Counter[str] = Counter()
    for game in games:
        game_id = _game_id(game)
        playtime = float(game.get("playtime") or 0)
        favorite = _is_favorite(game_id, category_names)
        if playtime <= 0 and not favorite:
            continue
        weight = max(playtime, 0.0)
        if favorite:
            weight += 5.0
        for category in category_names.get(game_id, []):
            if category not in {"all", "favorite"} and not category.startswith("."):
                weights[category] += weight
    return weights


def rank_locally(
    games: list[dict[str, Any]], allow_steam_review_fetch: bool = True
) -> tuple[list[dict[str, Any]], dict[str, Recommendation]]:
    apply_cached_steam_review_summaries(games, allow_fetch=allow_steam_review_fetch)
    game_ids = [_game_id(game) for game in games if _game_id(game)]
    category_names = categories_db.get_categories_in_games(game_ids)
    category_weights = _build_category_weights(games, category_names)

    recommendations: dict[str, Recommendation] = {}
    for game in games:
        game_id = _game_id(game)
        reasons: list[str] = []
        score = 0.0

        matched_categories = [
            category
            for category in category_names.get(game_id, [])
            if category in category_weights and category not in {"all", "favorite"} and not category.startswith(".")
        ]
        if matched_categories:
            score += sum(category_weights[category] for category in matched_categories)
            reasons.append("Matches categories you play often")

        bonus, reason = _steam_rating_bonus(game)
        if bonus:
            score += bonus
        if reason:
            reasons.append(reason)

        if game.get("installed"):
            score += 0.25
            reasons.append("Installed in your library")
        if _is_favorite(game_id, category_names):
            score += 1.0
            reasons.append("Favorite")

        recommendations[game_id] = Recommendation(game_id, score, tuple(dict.fromkeys(reasons)))

    def sort_key(game: dict[str, Any]):
        game_id = _game_id(game)
        recommendation = recommendations.get(game_id, Recommendation(game_id, 0.0, ()))
        return -recommendation.score, get_natural_sort_key(str(game.get("sortname") or game.get("name") or ""))

    return sorted(games, key=sort_key), recommendations


def _minimal_game_payload(game: dict[str, Any], category_names: dict[str, list[str]]) -> dict[str, Any]:
    game_id = _game_id(game)
    return {
        "id": game_id,
        "name": game.get("name"),
        "categories": [
            category
            for category in category_names.get(game_id, [])
            if category not in {"all"} and not category.startswith(".")
        ],
        "playtime": game.get("playtime") or 0,
        "favorite": "favorite" in category_names.get(game_id, []),
        "installed": bool(game.get("installed")),
        "steam_rating": game.get("steam_rating") or game.get("steam_review_summary"),
        "year": game.get("year"),
    }


def _reorder_with_gemini(candidates: list[dict[str, Any]]) -> list[str] | None:
    access_token = DEFAULT_LLM_PROVIDER.load_access_token()
    if not access_token:
        return None

    game_ids = [_game_id(game) for game in candidates if _game_id(game)]
    category_names = categories_db.get_categories_in_games(game_ids)
    payload = [_minimal_game_payload(game, category_names) for game in candidates]
    prompt = (
        "Rank these Lutris games for recommendation. Use only the provided fields. "
        "Return strict JSON as {\"recommendations\":[{\"id\":\"known-id\",\"reason\":\"short reason\"}]}. "
        "Do not add unknown ids.\n"
        + json.dumps(payload, ensure_ascii=True)
    )
    try:
        response = requests.post(
            GEMINI_GENERATE_CONTENT_URL,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        text = ""
        for candidate in data.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                text = str(part.get("text") or "")
                if text:
                    break
            if text:
                break
        if not text:
            raise ValueError("Gemini response did not contain text")
        data = json.loads(text)
    except Exception as ex:  # noqa: BLE001 - optional ranking must fall back
        logger.warning("Gemini recommendation ranking failed: %s", ex)
        return None

    known_ids = set(game_ids)
    ordered_ids = []
    for item in data.get("recommendations", []):
        item_id = str(item.get("id") or "")
        if item_id in known_ids and item_id not in ordered_ids:
            ordered_ids.append(item_id)
    return ordered_ids or None


def rank_recommended(
    games: list[dict[str, Any]], allow_steam_review_fetch: bool = True, allow_llm: bool = True
) -> tuple[list[dict[str, Any]], dict[str, Recommendation]]:
    local_games, recommendations = rank_locally(games, allow_steam_review_fetch=allow_steam_review_fetch)
    if not allow_llm:
        return local_games, recommendations
    llm_ids = _reorder_with_gemini(local_games[:MAX_LLM_CANDIDATES])
    if not llm_ids:
        return local_games, recommendations

    by_id = {_game_id(game): game for game in local_games}
    reordered = [by_id[game_id] for game_id in llm_ids if game_id in by_id]
    reordered_ids = set(llm_ids)
    reordered.extend(game for game in local_games if _game_id(game) not in reordered_ids)
    return reordered, recommendations
