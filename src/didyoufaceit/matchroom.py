"""Async matchroom analysis — yields SSE events with per-player map stats."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator

from .api import FaceitAPIError, FaceitClient
from .cache import (
    get_cached_matchroom,
    get_cached_player_stats,
    init_db,
    save_matchroom,
    save_player_stats,
    upsert_player,
)

log = logging.getLogger(__name__)

_LABEL_TO_MAP_ID = {
    "dust2": "de_dust2",
    "mirage": "de_mirage",
    "inferno": "de_inferno",
    "nuke": "de_nuke",
    "ancient": "de_ancient",
    "anubis": "de_anubis",
    "overpass": "de_overpass",
    # Not in current rotation — uncomment to re-enable:
    # "vertigo": "de_vertigo",
    # "train": "de_train",
    # "cache": "de_cache",
    # "cobblestone": "de_cobblestone",
}


def _label_to_map_id(label: str) -> str | None:
    key = label.lower().replace(" ", "").replace("_", "")
    return _LABEL_TO_MAP_ID.get(key)  # returns None for maps not in rotation


def _parse_map_stats(segments: list[dict]) -> dict[str, dict]:
    result = {}
    for seg in segments:
        if seg.get("type") != "Map" or seg.get("mode") != "5v5":
            continue
        label = seg.get("label", "")
        map_id = _label_to_map_id(label)
        if not map_id or map_id not in _LABEL_TO_MAP_ID.values():
            continue
        stats = seg.get("stats", {})
        try:
            wins = int(stats.get("Wins", 0))
            matches = int(stats.get("Matches", 0))
        except (ValueError, TypeError):
            continue
        if matches == 0:
            continue

        # Parse additional stats (all are strings in the API)
        def _float(key, default=0.0):
            try:
                return round(float(stats.get(key, default)), 2)
            except (ValueError, TypeError):
                return default

        def _int(key, default=0):
            try:
                return int(stats.get(key, default) or default)
            except (ValueError, TypeError):
                return default

        # Raw stats for HLTV 2.0 computation
        kills = _int("Kills")
        deaths = _int("Deaths")
        assists = _int("Assists")
        rounds = _int("Rounds")
        adr = _float("ADR")
        kd = _float("Average K/D Ratio")

        # Compute HLTV 2.0 approximation
        # Formula: 0.0073*KAST + 0.3591*KPR - 0.5329*DPR + 0.2372*Impact + 0.0032*ADR + 0.1587
        # Impact ≈ 2.13*KPR + 0.42*APR - 0.41
        # KAST not available — estimate from K/D (calibrated to FACEIT ranges: ~63-78%)
        rating = 0.0
        if rounds > 0:
            kpr = kills / rounds
            dpr = deaths / rounds
            apr = assists / rounds
            impact = 2.13 * kpr + 0.42 * apr - 0.41
            kast_est = max(55, min(85, 52 + 17 * kd)) if kd > 0 else 55
            rating = round(
                0.0073 * kast_est + 0.3591 * kpr - 0.5329 * dpr + 0.2372 * impact + 0.0032 * adr + 0.1587,
                2,
            )

        result[map_id] = {
            "wins": wins,
            "matches": matches,
            "wr": round(wins / matches * 100),
            "kd": kd,
            "rating": rating,
            "avg_kills": _float("Average Kills"),
            "avg_deaths": _float("Average Deaths"),
            "hs_pct": _float("Average Headshots %"),
            "adr": adr,
            "entry_rate": _float("Entry Rate"),
            "entry_success": _float("Entry Success Rate"),
            "win_streak": _int("Current Win Streak"),
            "longest_streak": _int("Longest Win Streak"),
        }
    return result


async def _fetch_player_stats(client: FaceitClient, pid: str, nick: str) -> dict:
    """Fetch player stats with cache. Returns {map_stats, source}."""
    cached = await get_cached_player_stats(pid)
    if cached:
        log.info("Stats cache hit for %s", nick)
        return {"map_stats": _parse_map_stats(cached.get("segments", [])), "source": "cache"}

    try:
        stats_raw = await client.get_player_stats(pid)
        await save_player_stats(pid, stats_raw)
        log.info("Stats fetched+cached for %s", nick)
        return {"map_stats": _parse_map_stats(stats_raw.get("segments", [])), "source": "api"}
    except Exception as e:
        log.warning("Could not fetch stats for %s: %s", nick, e)
        return {"map_stats": {}, "source": "error"}


async def stream_matchroom(
    client: FaceitClient,
    match_id: str,
) -> AsyncGenerator[dict, None]:
    await init_db()

    # ── Step 1: fetch matchroom (cached permanently) ──────────────────────────
    yield {"type": "status", "msg": "Fetching matchroom…"}

    cached_mr = await get_cached_matchroom(match_id)
    if cached_mr:
        match = cached_mr
        yield {"type": "status", "msg": "Loaded matchroom from cache"}
    else:
        try:
            match = await client.get_match(match_id)
            await save_matchroom(match_id, match)
        except FaceitAPIError as e:
            yield {"type": "error", "msg": f"Match not found: {e}"}
            return

    teams_raw = match.get("teams", {})
    if not teams_raw:
        yield {"type": "error", "msg": "No team data in match"}
        return

    # Collect all players
    factions: dict[str, list[dict]] = {}
    for faction, data in teams_raw.items():
        factions[faction] = []
        for p in data.get("roster", []):
            pid = p["player_id"]
            nick = p.get("nickname", pid[:8])
            elo = p.get("elo", 0)
            skill_level = p.get("game_skill_level", 0)
            factions[faction].append(
                {
                    "player_id": pid,
                    "nickname": nick,
                    "elo": elo,
                    "skill_level": skill_level,
                }
            )
            await upsert_player(pid, nick)

    # Map pool from voting
    voting = match.get("voting", {})
    map_pool = [e["game_map_id"] for e in voting.get("map", {}).get("entities", []) if "game_map_id" in e]
    picks = voting.get("map", {}).get("pick", [])
    picked_map = picks[0] if picks else None

    total_players = sum(len(v) for v in factions.values())
    yield {"type": "status", "msg": f"Fetching stats for {total_players} players…"}

    # ── Step 2: fetch all player stats concurrently ───────────────────────────
    all_players = [(faction, p) for faction, players in factions.items() for p in players]

    # Fetch all stats in parallel (respects httpx connection pool limits)
    stats_tasks = [_fetch_player_stats(client, p["player_id"], p["nickname"]) for _, p in all_players]
    stats_results = await asyncio.gather(*stats_tasks)

    # Emit player events and accumulate summary
    summary: dict[str, dict[str, dict]] = {f: {} for f in factions}

    for (faction, p), result in zip(all_players, stats_results):
        map_stats = result["map_stats"]

        yield {
            "type": "player",
            "faction": faction,
            "player_id": p["player_id"],
            "nickname": p["nickname"],
            "elo": p["elo"],
            "skill_level": p.get("skill_level", 0),
            "map_stats": map_stats,
        }

        for map_id, ms in map_stats.items():
            if map_id not in summary[faction]:
                summary[faction][map_id] = {"total_wins": 0, "total_matches": 0}
            summary[faction][map_id]["total_wins"] += ms["wins"]
            summary[faction][map_id]["total_matches"] += ms["matches"]

    # ── Step 3: compute team map scores ───────────────────────────────────────
    team_scores: dict[str, dict[str, dict]] = {}
    for faction, maps in summary.items():
        team_scores[faction] = {}
        for map_id, agg in maps.items():
            if agg["total_matches"] == 0:
                continue
            team_scores[faction][map_id] = {
                "total_wins": agg["total_wins"],
                "total_matches": agg["total_matches"],
                "wr": round(agg["total_wins"] / agg["total_matches"] * 100),
            }

    yield {
        "type": "summary",
        "factions": {
            faction: {
                "name": teams_raw[faction].get("name", faction),
                "map_scores": team_scores.get(faction, {}),
            }
            for faction in factions
        },
        "map_pool": map_pool,
        "picked_map": picked_map,
    }

    yield {"type": "done"}
