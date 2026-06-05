"""Game recommendation ranking with an optional LLM reordering pass."""

import hashlib
import json
import os
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import requests

from lutris import settings
from lutris.database import categories as categories_db
from lutris.database import games as games_db
from lutris.util.llm_auth import DEFAULT_LLM_PROVIDER
from lutris.util.log import logger
from lutris.util.steam_reviews import apply_cached_steam_review_summaries, get_steam_appid
from lutris.util.strings import get_natural_sort_key

MAX_LLM_CANDIDATES = 50
GEMINI_GENERATE_CONTENT_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
GEMINI_RECOMMENDATIONS_CACHE_PATH = os.path.join(settings.CACHE_DIR, "gemini-recommendations.json")
GEMINI_RECOMMENDATIONS_CACHE_TTL = 6 * 60 * 60


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


def _recommendation_cache_key(payload: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
    return digest


def _load_recommendation_cache() -> list[str] | None:
    """Return the cached Gemini ordering if it is still fresh.

    The cache is reused for its whole TTL even if the games change; we only
    want a single Gemini request, with every later ranking served from cache."""
    try:
        with open(GEMINI_RECOMMENDATIONS_CACHE_PATH, encoding="utf-8") as cache_file:
            cache_data = json.load(cache_file)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(cache_data, dict):
        return None

    updated_at = cache_data.get("updated_at")
    if not isinstance(updated_at, (int, float)) or time.time() - float(updated_at) > GEMINI_RECOMMENDATIONS_CACHE_TTL:
        return None

    ordered_ids = cache_data.get("ordered_ids")
    if not isinstance(ordered_ids, list):
        return None

    return [str(item) for item in ordered_ids if isinstance(item, str) and item]


def _save_recommendation_cache(cache_key: str, ordered_ids: list[str]) -> None:
    try:
        os.makedirs(os.path.dirname(GEMINI_RECOMMENDATIONS_CACHE_PATH), exist_ok=True)
        with open(GEMINI_RECOMMENDATIONS_CACHE_PATH, "w", encoding="utf-8") as cache_file:
            json.dump(
                {"cache_key": cache_key, "updated_at": time.time(), "ordered_ids": ordered_ids}, cache_file, indent=2
            )
    except OSError as ex:
        logger.warning("Failed to write Gemini recommendation cache: %s", ex)


def invalidate_recommendation_cache() -> None:
    try:
        os.unlink(GEMINI_RECOMMENDATIONS_CACHE_PATH)
    except FileNotFoundError:
        return
    except OSError as ex:
        logger.warning("Failed to remove Gemini recommendation cache: %s", ex)


def _is_full_library_payload(payload: list[dict[str, Any]]) -> bool:
    """True if the candidates cover the library, as far as MAX_LLM_CANDIDATES allows.

    During startup the view ranks before all games are loaded; spending the
    single Gemini request on such a partial list would poison the cache."""
    try:
        library_size = len(games_db.get_games())
    except Exception as ex:  # noqa: BLE001 - optional ranking must fall back
        logger.warning("Could not determine library size for Gemini ranking: %s", ex)
        return False
    return len(payload) >= min(MAX_LLM_CANDIDATES, library_size)


def _reorder_with_gemini(payload: list[dict[str, Any]]) -> list[str] | None:
    access_token = DEFAULT_LLM_PROVIDER.load_access_token()
    if not access_token:
        return None
    project_id = DEFAULT_LLM_PROVIDER.load_project_id()

    prompt = (
        "Rank these Lutris games for recommendation. Use only the provided fields. "
        'Return strict JSON as {"recommendations":[{"id":"known-id","reason":"short reason"}]}. '
        "Do not add unknown ids.\n" + json.dumps(payload, ensure_ascii=True)
    )
    try:
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        if project_id:
            headers["x-goog-user-project"] = project_id
        response = requests.post(
            GEMINI_GENERATE_CONTENT_URL,
            headers=headers,
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
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
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
            text = text.rsplit("```", 1)[0]
        data = json.loads(text)
    except requests.HTTPError as ex:
        body = ex.response.text[:300] if ex.response is not None else ""
        logger.warning("Gemini recommendation ranking failed: %s %s", ex, body)
        return None
    except Exception as ex:  # noqa: BLE001 - optional ranking must fall back
        logger.warning("Gemini recommendation ranking failed: %s", ex)
        return None

    known_ids = {str(item["id"]) for item in payload if item.get("id")}
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
    candidates = local_games[:MAX_LLM_CANDIDATES]
    candidate_ids = [_game_id(game) for game in candidates if _game_id(game)]
    category_names = categories_db.get_categories_in_games(candidate_ids)
    payload = [_minimal_game_payload(game, category_names) for game in candidates]
    llm_ids = _load_recommendation_cache()
    if llm_ids is None and payload and _is_full_library_payload(payload):
        llm_ids = _reorder_with_gemini(payload)
        if llm_ids:
            _save_recommendation_cache(_recommendation_cache_key(payload), llm_ids)
    if not llm_ids:
        return local_games, recommendations

    by_id = {_game_id(game): game for game in local_games}
    reordered = [by_id[game_id] for game_id in llm_ids if game_id in by_id]
    reordered_ids = set(llm_ids)
    reordered.extend(game for game in local_games if _game_id(game) not in reordered_ids)
    return reordered, recommendations
