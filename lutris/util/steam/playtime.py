"""Import playtimes recorded by the Steam client into the Lutris library"""

import os
from dataclasses import dataclass, field
from typing import Any

from lutris import settings
from lutris.database import sql
from lutris.database.games import get_games
from lutris.database.services import ServiceGameCollection
from lutris.util.log import logger
from lutris.util.steam.config import get_user_data_dirs
from lutris.util.steam.vdfutils import vdf_parse


@dataclass
class SteamPlaytime:
    """Playtime Steam recorded for a single app"""

    appid: str
    minutes: int = 0
    lastplayed: int = 0

    @property
    def hours(self) -> float:
        return self.minutes / 60


@dataclass
class PlaytimeUpdate:
    """A pending playtime change for a game already in the Lutris library"""

    game_id: int
    name: str
    playtime: float | None  # in hours; None if Steam has nothing newer
    lastplayed: int | None  # Unix timestamp; None if Steam has nothing newer


@dataclass
class PlaytimeAddition:
    """A Steam library game with playtime that is absent from the Lutris library"""

    appid: str
    name: str
    slug: str
    playtime: float  # in hours
    lastplayed: int  # Unix timestamp; 0 if unknown


@dataclass
class PlaytimeImportCandidates:
    """Everything a Steam playtime import would change, for preview and application"""

    updates: list[PlaytimeUpdate] = field(default_factory=list)
    additions: list[PlaytimeAddition] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.updates or self.additions)

    @property
    def updated_hours(self) -> float:
        return sum(update.playtime or 0 for update in self.updates)

    @property
    def added_hours(self) -> float:
        return sum(addition.playtime for addition in self.additions)


def _get_entry_case_insensitive(config_dict: dict[str, Any], path: list[str]) -> Any:
    for key, value in config_dict.items():
        if key.lower() == path[0].lower():
            if len(path) <= 1:
                return value
            return _get_entry_case_insensitive(value, path[1:])
    raise KeyError(path[0])


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def get_steam_playtimes() -> dict[str, SteamPlaytime]:
    """Return the playtimes Steam recorded per AppID, merged over all local Steam users.
    Only apps with a playtime or last played time are returned."""
    playtimes: dict[str, SteamPlaytime] = {}
    userdata_path, user_ids = get_user_data_dirs()
    for user_id in user_ids:
        config_path = os.path.join(userdata_path, user_id, "config/localconfig.vdf")
        if not os.path.exists(config_path):
            continue
        try:
            with open(config_path, "r", encoding="utf-8") as config_file:
                config = vdf_parse(config_file, {})
            apps = _get_entry_case_insensitive(config, ["UserLocalConfigStore", "Software", "Valve", "Steam", "apps"])
        except (KeyError, OSError, ValueError) as ex:
            logger.warning("Unable to read Steam playtimes from %s: %s", config_path, ex)
            continue
        for appid, app_data in apps.items():
            if not isinstance(app_data, dict):
                continue
            entries = {key.lower(): value for key, value in app_data.items()}
            playtime = playtimes.setdefault(appid, SteamPlaytime(appid))
            playtime.minutes = max(playtime.minutes, _to_int(entries.get("playtime")))
            playtime.lastplayed = max(playtime.lastplayed, _to_int(entries.get("lastplayed")))
    return {appid: playtime for appid, playtime in playtimes.items() if playtime.minutes or playtime.lastplayed}


def get_import_candidates() -> PlaytimeImportCandidates:
    """Compare Steam's recorded playtimes against the Lutris library and return
    the games that would gain playtime from an import."""
    candidates = PlaytimeImportCandidates()
    playtimes = get_steam_playtimes()
    if not playtimes:
        return candidates
    library_appids = set()
    for db_game in get_games(filters={"service": "steam"}):
        appid = str(db_game["service_id"])
        library_appids.add(appid)
        steam_playtime = playtimes.get(appid)
        if not steam_playtime:
            continue
        playtime = steam_playtime.hours if steam_playtime.hours > (db_game["playtime"] or 0) else None
        lastplayed = steam_playtime.lastplayed if steam_playtime.lastplayed > (db_game["lastplayed"] or 0) else None
        if playtime or lastplayed:
            candidates.updates.append(PlaytimeUpdate(int(db_game["id"]), db_game["name"], playtime, lastplayed))
    for service_game in ServiceGameCollection.get_for_service("steam"):
        appid = str(service_game["appid"])
        steam_playtime = playtimes.get(appid)
        if appid in library_appids or not steam_playtime or not steam_playtime.minutes:
            continue
        candidates.additions.append(
            PlaytimeAddition(
                appid=appid,
                name=str(service_game["name"]),
                slug=str(service_game["slug"]),
                playtime=steam_playtime.hours,
                lastplayed=steam_playtime.lastplayed,
            )
        )
    return candidates


def apply_import(candidates: PlaytimeImportCandidates, add_uninstalled: bool = True) -> dict[str, int]:
    """Write the candidate playtimes to the database. Games missing from the library
    are added as non-installed games unless add_uninstalled is False.
    Returns the number of updated and added games."""
    for update in candidates.updates:
        updated_fields: sql.DBUpdateDict = {}
        if update.playtime is not None:
            updated_fields["playtime"] = update.playtime
        if update.lastplayed is not None:
            updated_fields["lastplayed"] = update.lastplayed
        sql.db_update(settings.DB_PATH, "games", updated_fields, conditions={"id": update.game_id})
    added = 0
    if add_uninstalled:
        for addition in candidates.additions:
            sql.db_insert(
                settings.DB_PATH,
                "games",
                {
                    "name": addition.name,
                    "slug": addition.slug,
                    "installed": 0,
                    "service": "steam",
                    "service_id": addition.appid,
                    "playtime": addition.playtime,
                    "lastplayed": addition.lastplayed or None,
                },
            )
            added += 1
    logger.info("Steam playtime import: %s games updated, %s games added", len(candidates.updates), added)
    return {"updated": len(candidates.updates), "added": added}
