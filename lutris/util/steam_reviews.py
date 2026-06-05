"""Cached Steam review summaries for recommendation scoring."""

import json
import os
import time
from typing import Any

import requests

from lutris import settings
from lutris.util.log import logger

STEAM_REVIEWS_CACHE_PATH = os.path.join(settings.CACHE_DIR, "steam-review-summaries.json")
STEAM_REVIEWS_CACHE_TTL = 7 * 24 * 60 * 60
MAX_STEAM_REVIEW_FETCHES = 100


def _load_cache() -> dict[str, dict[str, Any]]:
    try:
        with open(STEAM_REVIEWS_CACHE_PATH, "r", encoding="utf-8") as cache_file:
            data = json.load(cache_file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(cache: dict[str, dict[str, Any]]) -> None:
    os.makedirs(settings.CACHE_DIR, exist_ok=True)
    try:
        with open(STEAM_REVIEWS_CACHE_PATH, "w", encoding="utf-8") as cache_file:
            json.dump(cache, cache_file)
    except OSError as ex:
        logger.warning("Failed to save Steam review cache: %s", ex)


def _fetch_review_summary(appid: str) -> str | None:
    url = f"https://store.steampowered.com/appreviews/{appid}"
    try:
        response = requests.get(
            url,
            params={"json": 1, "filter": "summary", "language": "all", "purchase_type": "all"},
            timeout=2,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as ex:  # noqa: BLE001 - review data is optional
        logger.warning("Failed to fetch Steam review summary for %s: %s", appid, ex)
        return None

    summary = data.get("query_summary") or {}
    rating = summary.get("review_score_desc")
    if rating:
        return str(rating)
    if summary.get("total_reviews") == 0:
        return "No user reviews"
    return None


def get_steam_appid(game: dict[str, Any]) -> str | None:
    if game.get("service") == "steam" and game.get("service_id"):
        return str(game["service_id"])
    if game.get("appid"):
        return str(game["appid"])
    return None


def apply_cached_steam_review_summaries(games: list[dict[str, Any]], allow_fetch: bool = True) -> None:
    cache = _load_cache()
    now = time.time()
    fetched = 0
    changed = False

    for game in games:
        if game.get("steam_rating") or game.get("steam_review_summary"):
            continue
        appid = get_steam_appid(game)
        if not appid:
            continue

        cached = cache.get(appid)
        if cached and now - float(cached.get("updated_at") or 0) < STEAM_REVIEWS_CACHE_TTL:
            rating = cached.get("rating")
        elif allow_fetch and fetched < MAX_STEAM_REVIEW_FETCHES:
            rating = _fetch_review_summary(appid)
            cache[appid] = {"rating": rating, "updated_at": now}
            fetched += 1
            changed = True
        else:
            rating = cached.get("rating") if cached else None

        if rating:
            game["steam_review_summary"] = rating

    if changed:
        _save_cache(cache)
