"""Turning FACEIT match payloads into cached match rows, shared by search and ban scan.

A match row: {match_id, started_at, status, map, teams: {faction: [player_id]}, score:
{faction: int}, faceit_url, competition_type, competition_name}.

- History items (/players/{id}/history) already carry rosters, series score and timestamps,
  so a whole page of matches is stored with no extra calls. They lack the map.
- /matches/{id}/stats adds the map and the round score, one call per match, only for the
  matches someone is about to look at.
- /matches/{id} is the fallback for a match we have no row for.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .api import FaceitAPIError, FaceitClient
from .cache import (
    FINAL_MATCH_STATUSES,
    STATS_MISSING_AFTER_SECONDS,
    get_cached_matches_bulk,
    save_match_details,
    save_matches_bulk,
    upsert_players_bulk,
)
from .maps import normalize_map_id

log = logging.getLogger(__name__)

DETAIL_BATCH = 20  # match calls in flight per batch (the client's semaphore caps it further)


def _url(raw: dict) -> str:
    return (raw.get("faceit_url") or "").replace("{lang}", "en")


def match_from_history_item(item: dict) -> tuple[dict, list[tuple]]:
    """(match row, player tuples for upsert) from one history item."""
    teams, players = {}, []
    for faction, data in (item.get("teams") or {}).items():
        roster = data.get("players") or []
        teams[faction] = [p["player_id"] for p in roster if p.get("player_id")]
        players += [
            (p["player_id"], p.get("nickname", ""), p.get("game_player_id", ""), p.get("avatar", ""))
            for p in roster
            if p.get("player_id")
        ]
    match = {
        "match_id": item["match_id"],
        "started_at": item.get("started_at") or 0,
        "status": (item.get("status") or "unknown").lower(),
        "map": "unknown",
        "teams": teams,
        "score": (item.get("results") or {}).get("score") or {},
        "faceit_url": _url(item),
        "competition_type": item.get("competition_type") or "",
        "competition_name": item.get("competition_name") or "",
    }
    return match, players


def match_from_details(raw: dict) -> tuple[dict, list[tuple]]:
    """(match row, player tuples) from /matches/{id}."""
    teams, players = {}, []
    for faction, data in (raw.get("teams") or {}).items():
        roster = data.get("roster") or []
        teams[faction] = [p["player_id"] for p in roster if p.get("player_id")]
        players += [
            (p["player_id"], p.get("nickname", ""), p.get("game_player_id", ""), p.get("avatar", ""))
            for p in roster
            if p.get("player_id")
        ]
    picks = ((raw.get("voting") or {}).get("map") or {}).get("pick") or []
    match = {
        "match_id": raw["match_id"],
        "started_at": raw.get("started_at") or 0,
        "status": (raw.get("status") or "unknown").lower(),
        "map": normalize_map_id(picks[0]) if picks else "unknown",
        "teams": teams,
        "score": (raw.get("results") or {}).get("score") or {},
        "faceit_url": _url(raw),
        "competition_type": raw.get("competition_type") or "",
        "competition_name": raw.get("competition_name") or "",
    }
    return match, players


def details_from_stats(stats: dict, teams: dict[str, list[str]]) -> tuple[str | None, dict | None]:
    """(map, score) from /matches/{id}/stats, with teams matched to factions by roster.

    One map: that map and its round score. Several (BO3): the maps joined with " / " and the
    series score as maps won, since one map's rounds don't describe the series.
    """
    rounds = stats.get("rounds") or []
    if not rounds:
        return None, None

    def faction_of(team: dict) -> str | None:
        ids = {p.get("player_id") for p in team.get("players") or []}
        return next((f for f, roster in teams.items() if ids & set(roster)), None)

    maps = [normalize_map_id((r.get("round_stats") or {}).get("Map")) or "unknown" for r in rounds]
    score: dict[str, int] = {}
    for r in rounds:
        for team in r.get("teams") or []:
            faction = faction_of(team)
            if faction is None:
                return " / ".join(maps), None
            ts = team.get("team_stats") or {}
            try:
                value = int(ts.get("Final Score" if len(rounds) == 1 else "Team Win", 0))
            except (TypeError, ValueError):
                return " / ".join(maps), None
            score[faction] = score.get(faction, 0) + value
    return " / ".join(maps), score if len(score) == 2 else None


async def store_history_items(items: list[dict]) -> list[str]:
    """Save a page of history items as match rows (and their players). Returns the match ids."""
    rows, players = [], []
    for item in items:
        if not item.get("match_id"):
            continue
        match, ps = match_from_history_item(item)
        rows.append(match)
        players += ps
    await save_matches_bulk(rows)
    await upsert_players_bulk(players)
    return [r["match_id"] for r in rows]


async def load_matches(
    client: FaceitClient, match_ids: list[str], *, with_details: bool, counter: dict | None = None
) -> dict[str, dict]:
    """Rows for these ids, fetching what's missing.

    Missing rows come from /matches/{id}. With with_details, rows without a map and round score
    get one /matches/{id}/stats call each; a 404 is remembered so it isn't retried.
    counter["fetched"] is increased by the number of FACEIT calls made.
    """
    counter = counter if counter is not None else {}
    counter.setdefault("fetched", 0)
    rows = {mid: m for mid, m in (await get_cached_matches_bulk(match_ids)).items() if m}

    missing = [mid for mid in match_ids if mid not in rows]
    for i in range(0, len(missing), DETAIL_BATCH):
        batch = missing[i : i + DETAIL_BATCH]
        results = await asyncio.gather(*(client.get_match(mid) for mid in batch), return_exceptions=True)
        counter["fetched"] += len(batch)
        new, players = [], []
        for mid, raw in zip(batch, results):
            if isinstance(raw, BaseException):
                log.warning("Could not fetch match %s: %s", mid, raw)
                continue
            match, ps = match_from_details(raw)
            new.append(match)
            players += ps
        await save_matches_bulk(new)
        await upsert_players_bulk(players)
        rows.update((m["match_id"], {**m, "stats_fetched": False}) for m in new)

    if with_details:
        todo = [mid for mid in match_ids if mid in rows and not rows[mid].get("stats_fetched")]
        for i in range(0, len(todo), DETAIL_BATCH):
            batch = todo[i : i + DETAIL_BATCH]
            results = await asyncio.gather(*(client.get_match_stats(mid) for mid in batch), return_exceptions=True)
            counter["fetched"] += len(batch)
            for mid, stats in zip(batch, results):
                row = rows[mid]
                if isinstance(stats, FaceitAPIError) and stats.status_code == 404:
                    stats = {}  # same as "no rounds": maybe not published yet, maybe never
                elif isinstance(stats, BaseException):
                    log.warning("Could not fetch stats for %s: %s", mid, stats)
                    continue
                map_name, score = details_from_stats(stats, row["teams"])
                if map_name is None and score is None and not _stats_gone_for_good(row):
                    continue  # just finished: FACEIT publishes stats a little later, ask again then
                await save_match_details(mid, map_name, score)
                if map_name:
                    row["map"] = map_name
                if score:
                    row["score"] = score
                row["stats_fetched"] = True
    return rows


def _stats_gone_for_good(row: dict) -> bool:
    """No stats for a cancelled match, or one that started over a day ago, won't appear later."""
    if (row.get("status") or "").upper() in FINAL_MATCH_STATUSES - {"FINISHED"}:
        return True
    return time.time() - (row.get("started_at") or 0) > STATS_MISSING_AFTER_SECONDS


def score_str(score: dict, your_team: str | None) -> str:
    """'X / Y' from your side, or 'N/A'."""
    if not score or not your_team or score.get(your_team) is None:
        return "N/A"
    others = [v for k, v in score.items() if k != your_team]
    return f"{score[your_team]} / {others[0] if others else '?'}"
