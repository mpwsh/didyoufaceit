"""Matchroom analysis — yields SSE events with per-player map stats."""

from __future__ import annotations
import logging
from typing import Generator

from .api import FaceitClient, FaceitAPIError
from .cache import (
    upsert_player, get_nicknames, init_db,
    get_cached_matchroom, save_matchroom,
    get_cached_player_stats, save_player_stats,
)

log = logging.getLogger(__name__)

# Maps we care about (current active pool)
KNOWN_MAPS = {
    "de_ancient", "de_anubis", "de_dust2", "de_inferno",
    "de_mirage",  "de_nuke",   "de_train", "de_vertigo",
}


# FACEIT label → map_id (handles non-obvious ones like "Dust2" → "de_dust2")
_LABEL_TO_MAP_ID = {
    "dust2":   "de_dust2",
    "mirage":  "de_mirage",
    "inferno": "de_inferno",
    "nuke":    "de_nuke",
    "ancient": "de_ancient",
    "anubis":  "de_anubis",
    "vertigo": "de_vertigo",
    "train":   "de_train",
    "cache":   "de_cache",
    "overpass":"de_overpass",
    "cobblestone": "de_cobblestone",
}

def _label_to_map_id(label: str) -> str | None:
    key = label.lower().replace(" ", "").replace("_", "")
    # Try explicit map first, then fall back to de_ prefix
    if key in _LABEL_TO_MAP_ID:
        return _LABEL_TO_MAP_ID[key]
    # Generic fallback: de_<key> — handles any future maps
    return f"de_{key}"


def _parse_map_stats(segments: list[dict]) -> dict[str, dict]:
    """
    From /players/{id}/stats/cs2 segments, extract per-map stats.
    Returns {map_id: {wins, matches, wr}} — only 5v5 map segments.
    """
    result = {}
    for seg in segments:
        if seg.get("type") != "Map" or seg.get("mode") != "5v5":
            continue
        label  = seg.get("label", "")
        map_id = _label_to_map_id(label)
        if not map_id:
            continue
        stats = seg.get("stats", {})
        try:
            wins    = int(stats.get("Wins", 0))
            matches = int(stats.get("Matches", 0))
        except (ValueError, TypeError):
            continue
        if matches == 0:
            continue
        result[map_id] = {
            "wins":    wins,
            "matches": matches,
            "wr":      round(wins / matches * 100),
        }
    return result


def stream_matchroom(
    client: FaceitClient,
    match_id: str,
) -> Generator[dict, None, None]:
    """
    Yields SSE events:
      {"type": "status",   "msg": "..."}
      {"type": "player",   "faction": "faction1"|"faction2", "player_id": ..., "nickname": ...,
                           "elo": ..., "map_stats": {map_id: {wins, matches, wr, wrc}}}
      {"type": "summary",  "faction1": {map_id: team_score}, "faction2": {...},
                           "map_pool": [...], "picked_map": "de_anubis"|None}
      {"type": "done"}
      {"type": "error",    "msg": "..."}
    """
    init_db()

    # ── Step 1: fetch matchroom (cached permanently — roster is immutable) ────
    yield {"type": "status", "msg": "Fetching matchroom…"}

    cached_mr = get_cached_matchroom(match_id)
    if cached_mr:
        match = cached_mr
        yield {"type": "status", "msg": "Loaded matchroom from cache"}
    else:
        try:
            match = client.get_match(match_id)
            save_matchroom(match_id, match)
        except FaceitAPIError as e:
            yield {"type": "error", "msg": f"Match not found: {e}"}
            return

    teams_raw = match.get("teams", {})
    if not teams_raw:
        yield {"type": "error", "msg": "No team data in match"}
        return

    # Collect all players: {faction: [{player_id, nickname, elo}]}
    factions: dict[str, list[dict]] = {}
    for faction, data in teams_raw.items():
        factions[faction] = []
        for p in data.get("roster", []):
            pid  = p["player_id"]
            nick = p.get("nickname", pid[:8])
            elo  = p.get("elo", 0)
            skill_level = p.get("game_skill_level", 0)
            factions[faction].append({"player_id": pid, "nickname": nick, "elo": elo, "skill_level": skill_level})
            upsert_player(pid, nick)

    # Map pool from voting
    voting    = match.get("voting", {})
    map_pool  = [e["game_map_id"] for e in voting.get("map", {}).get("entities", []) if "game_map_id" in e]
    picks     = voting.get("map", {}).get("pick", [])
    picked_map = picks[0] if picks else None

    total_players = sum(len(v) for v in factions.values())
    yield {"type": "status", "msg": f"Fetching stats for {total_players} players…"}

    # ── Step 2: fetch each player's stats ────────────────────────────────────
    # summary accumulates {faction: {map_id: [wrc values]}} for team score
    summary: dict[str, dict[str, list[float]]] = {f: {} for f in factions}

    for faction, players in factions.items():
        for p in players:
            pid  = p["player_id"]
            nick = p["nickname"]
            yield {"type": "status", "msg": f"Loading stats for {nick}…"}

            try:
                # Check cache first (3-day TTL)
                cached_stats = get_cached_player_stats(pid)
                if cached_stats:
                    map_stats = _parse_map_stats(cached_stats.get("segments", []))
                    log.info("Stats cache hit for %s", nick)
                else:
                    stats_raw = client._get(f"/players/{pid}/stats/cs2")
                    save_player_stats(pid, stats_raw)
                    map_stats = _parse_map_stats(stats_raw.get("segments", []))
                    log.info("Stats fetched+cached for %s", nick)
            except Exception as e:
                log.warning("Could not fetch stats for %s: %s", nick, e)
                map_stats = {}

            # Calculate WRc for each map:
            # WRc = wins / total_team_games_on_map_across_all_players
            # We store raw wins+matches here; WRc is recalculated in summary after all players fetched
            # For the player event, send raw stats — frontend calculates WRc per team

            yield {
                "type":        "player",
                "faction":     faction,
                "player_id":   pid,
                "nickname":    nick,
                "elo":         p["elo"],
                "skill_level": p.get("skill_level", 0),
                "map_stats":   map_stats,   # {map_id: {wins, matches, wr}}
            }

            # Accumulate for team summary
            for map_id, ms in map_stats.items():
                if map_id not in summary[faction]:
                    summary[faction][map_id] = {"total_wins": 0, "total_matches": 0}
                summary[faction][map_id]["total_wins"]    += ms["wins"]
                summary[faction][map_id]["total_matches"] += ms["matches"]

    # ── Step 3: compute team map scores ──────────────────────────────────────
    # Team score for a map = total_wins / total_matches (weighted WR across all players)
    team_scores: dict[str, dict[str, dict]] = {}
    for faction, maps in summary.items():
        team_scores[faction] = {}
        for map_id, agg in maps.items():
            if agg["total_matches"] == 0:
                continue
            team_scores[faction][map_id] = {
                "total_wins":    agg["total_wins"],
                "total_matches": agg["total_matches"],
                "wr": round(agg["total_wins"] / agg["total_matches"] * 100),
            }

    yield {
        "type":      "summary",
        "factions":  {
            faction: {
                "name": teams_raw[faction].get("name", faction),
                "map_scores": team_scores.get(faction, {}),
            }
            for faction in factions
        },
        "map_pool":   map_pool,
        "picked_map": picked_map,
    }

    yield {"type": "done"}
