"""Async matchroom analysis — yields SSE events with per-player map stats."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator

from .api import FaceitAPIError, FaceitClient
from .cache import (
    FINAL_MATCH_STATUSES,
    get_cached_matchroom,
    get_cached_player_recent,
    get_cached_player_stats,
    save_matchroom,
    save_player_recent,
    save_player_stats,
    upsert_players_bulk,
)
from .maps import ACTIVE_POOL, map_display_name, normalize_map_id
from .rating import RECENT_MATCHES, lifetime_map_rating, recent_form

log = logging.getLogger(__name__)


def _parse_map_stats(segments: list[dict]) -> dict[str, dict]:
    """Lifetime 5v5 stats per map, for every map the player has played. Pool filtering happens in the UI."""
    result = {}
    for seg in segments:
        if seg.get("type") != "Map" or seg.get("mode") != "5v5":
            continue
        map_id = normalize_map_id(seg.get("label", ""))
        stats = seg.get("stats", {})
        try:
            wins = int(stats.get("Wins", 0))
            matches = int(stats.get("Matches", 0))
        except (ValueError, TypeError):
            continue
        if not map_id or matches == 0:
            continue
        rating, kd, adr = lifetime_map_rating(stats)
        result[map_id] = {
            "wins": wins,
            "matches": matches,
            "wr": round(wins / matches * 100),
            "kd": kd,
            "adr": adr,
            "rating": rating,
            "rating_lifetime": rating,
        }
    return result


def _merge_player_stats(segments: list[dict], recent_rows: list[dict] | None) -> dict:
    """Lifetime win rates and match counts, with the rating, K/D and ADR from recent form.

    Returns {"map_stats": {map_id: {...}}, "overall_rating": float | None}.
    Without recent rows (fetch failed), every number falls back to lifetime.
    """
    map_stats = _parse_map_stats(segments)
    if not recent_rows:
        overall = None
        if map_stats:
            weights = sum(m["matches"] for m in map_stats.values())
            overall = round(sum(m["rating"] * m["matches"] for m in map_stats.values()) / weights, 2)
        return {"map_stats": map_stats, "overall_rating": overall}

    form = recent_form(recent_rows)
    overall = form["overall"]["rating"] or None
    for map_id, recent in form["maps"].items():
        ms = map_stats.setdefault(
            map_id,
            # map played recently but missing from cached lifetime stats
            {"wins": recent["wins"], "matches": recent["matches"], "wr": 0, "rating_lifetime": None},
        )
        if not ms["wr"] and ms["matches"]:
            ms["wr"] = round(ms["wins"] / ms["matches"] * 100)
        ms.update(
            rating=recent["rating"],
            rating_recent_raw=recent["rating_raw"],
            recent_matches=recent["matches"],
            kd=recent["kd"],
            adr=recent["adr"],
        )
    # Maps not played recently: halfway between the (old) lifetime rating and current form
    for ms in map_stats.values():
        if "recent_matches" not in ms:
            ms["recent_matches"] = 0
            if overall and ms.get("rating_lifetime"):
                ms["rating"] = round((ms["rating_lifetime"] + overall) / 2, 2)
    return {"map_stats": map_stats, "overall_rating": overall}


def _map_pool_from_match(match: dict) -> tuple[list[str], dict[str, str], str | None]:
    """(pool, display names, picked map). FACEIT sends the queue's pool with each match;
    fall back to maps.ACTIVE_POOL when the match has no voting data."""
    voting_map = (match.get("voting") or {}).get("map") or {}
    names: dict[str, str] = {}
    for e in voting_map.get("entities", []):
        map_id = normalize_map_id(e.get("game_map_id") or e.get("class_name"))
        if map_id:
            names[map_id] = e.get("name") or map_display_name(map_id)
    pool = list(names) or list(ACTIVE_POOL)
    for map_id in pool:
        names.setdefault(map_id, map_display_name(map_id))
    picks = voting_map.get("pick") or []
    return pool, names, normalize_map_id(picks[0]) if picks else None


def match_result(match: dict) -> dict | None:
    """Winner and score of a finished match, from the match itself (`results`): rounds for a
    BO1, maps won for a series. None while it's live, or if FACEIT sent no usable result."""
    if str(match.get("status") or "").upper() != "FINISHED":
        return None
    results = match.get("results") or {}
    try:
        score = {str(f): int(v) for f, v in (results.get("score") or {}).items()}
    except (TypeError, ValueError):
        score = {}
    winner = results.get("winner")
    if len(score) != 2 and not winner:
        return None
    return {"winner": winner, "score": score if len(score) == 2 else None}


def _has_pool(match: dict) -> bool:
    entities = ((match.get("voting") or {}).get("map") or {}).get("entities") or []
    return any(normalize_map_id(e.get("game_map_id") or e.get("class_name")) for e in entities)


def map_pool_info(match: dict) -> dict:
    """The map pool fields the page needs, and whether the pool is the room's own or the
    default. A live room usually has no voting data until the veto starts, so the page
    shows the default pool with a note and re-checks until FACEIT sends the real one."""
    pool, names, picked = _map_pool_from_match(match)
    return {
        "map_pool": pool,
        "map_names": names,
        "picked_map": picked,
        "map_pool_source": "match" if _has_pool(match) else "default",
        "match_status": str(match.get("status") or "").upper(),
    }


async def _fetch_match(client: FaceitClient, match_id: str) -> dict:
    """The match, from cache once it's over; live rooms change during veto so they're always fresh."""
    cached = await get_cached_matchroom(match_id)
    if cached:
        return cached
    match = await client.get_match(match_id)
    if str(match.get("status", "")).upper() in FINAL_MATCH_STATUSES:
        await save_matchroom(match_id, match)
    elif not _has_pool(match):
        # Live room, veto not over: the Data API has no pool yet, FACEIT's site may
        web = await client.get_match_web(match_id)
        voting = _site_voting(web) if web else None
        if voting:
            match = {**match, "voting": voting}
    return match


_TAG_POOL = re.compile(r"^(?:de|cs|ar)_[a-z0-9_]+(?:,(?:de|cs|ar)_[a-z0-9_]+)+$")


def _site_voting(site: dict) -> dict | None:
    """The room's map pool from FACEIT's site payload, as a Data API style `voting` block.

    During the veto the site has no `voting` yet (state VOTING). The pool this room votes on
    (after players' map preferences) is in matchCustom.tree.map.values.value, same entity shape,
    and again as a tag like "de_dust2,de_nuke,…". After the veto, `voting` itself is there."""
    if _has_pool(site):
        return site["voting"]
    tree_map = (((site.get("matchCustom") or {}).get("tree") or {}).get("map") or {}).get("values") or {}
    entities = [e for e in tree_map.get("value") or [] if isinstance(e, dict)]
    if not entities:
        for tag in site.get("tags") or []:
            if isinstance(tag, str) and _TAG_POOL.match(tag):
                entities = [{"game_map_id": m} for m in tag.split(",")]
                break
    voting = {"map": {"entities": entities}}
    return voting if _has_pool({"voting": voting}) else None


async def _fetch_player_stats(client: FaceitClient, pid: str, nick: str) -> dict:
    """Lifetime stats (cached 3 days) plus the last RECENT_MATCHES matches (cached 6 hours).

    Returns {map_stats, overall_rating, source}. Two calls per uncached player.
    """

    async def lifetime() -> dict | None:
        cached = await get_cached_player_stats(pid)
        if cached:
            return cached
        raw = await client.get_player_stats(pid)
        await save_player_stats(pid, raw)
        return raw

    async def recent() -> list[dict] | None:
        cached = await get_cached_player_recent(pid)
        if cached is not None:
            return cached
        raw = await client.get_player_match_stats(pid, offset=0, limit=RECENT_MATCHES)
        rows = [item.get("stats", item) for item in raw.get("items", [])]
        await save_player_recent(pid, rows)
        return rows

    life, rows = await asyncio.gather(lifetime(), recent(), return_exceptions=True)
    if isinstance(life, BaseException):
        log.warning("Could not fetch lifetime stats for %s: %s", nick, life)
        life = None
    if isinstance(rows, BaseException):
        log.warning("Could not fetch recent matches for %s: %s", nick, rows)
        rows = None
    if life is None and rows is None:
        return {"map_stats": {}, "overall_rating": None, "source": "error"}
    merged = _merge_player_stats((life or {}).get("segments", []), rows)
    return {**merged, "source": "recent" if rows else "lifetime"}


async def stream_matchroom(
    client: FaceitClient,
    match_id: str,
) -> AsyncGenerator[dict, None]:
    # ── Step 1: fetch matchroom (cached once it's over; live rooms change during veto) ──
    yield {"type": "status", "msg": "Fetching matchroom…"}

    try:
        match = await _fetch_match(client, match_id)
    except FaceitAPIError as e:
        if e.status_code == 404:
            yield {"type": "error", "status": 404, "msg": "Match not found"}
            return
        raise

    teams_raw = match.get("teams", {})
    if not teams_raw:
        yield {"type": "error", "status": 422, "msg": "This match has no teams yet"}
        return

    # Collect all players
    factions: dict[str, list[dict]] = {}
    known: list[tuple] = []
    for faction, data in teams_raw.items():
        factions[faction] = []
        for p in data.get("roster", []):
            pid = p["player_id"]
            nick = p.get("nickname", pid[:8])
            factions[faction].append(
                {
                    "player_id": pid,
                    "nickname": nick,
                    "elo": p.get("elo", 0),
                    "skill_level": p.get("game_skill_level", 0),
                    "steam_id_64": p.get("game_player_id", ""),
                }
            )
            known.append((pid, nick, p.get("game_player_id", ""), p.get("avatar", "")))
    await upsert_players_bulk(known)

    pool_info = map_pool_info(match)

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
            "steam_id_64": p.get("steam_id_64", ""),
            "map_stats": map_stats,
            "overall_rating": result.get("overall_rating"),
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
        **pool_info,
        "result": match_result(match),
    }

    yield {"type": "done"}


async def match_pool(client: FaceitClient, match_id: str) -> dict:
    """Just the pool and veto state, for the page to re-check a live room cheaply (one call)."""
    return map_pool_info(await _fetch_match(client, match_id))
