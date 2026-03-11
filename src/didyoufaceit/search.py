"""Async search logic — yields SSE-style events as matches are discovered."""

from __future__ import annotations
import asyncio
import logging
from typing import AsyncGenerator, Iterator

from .api import FaceitClient
from .cache import (
    init_db, get_cached_match, save_match, update_match_score,
    link_player_match, link_player_matches_bulk,
    get_player_match_ids, is_player_fully_scanned, mark_player_scanned,
    upsert_player, upsert_players_bulk, get_nicknames,
)

log = logging.getLogger(__name__)

GAME      = "cs2"
PAGE_SIZE = 100
MAX_PAGES = 10
BATCH_SIZE = 10  # parallel match fetches per batch


# ── helpers ───────────────────────────────────────────────────────────────────

def _players_in_raw(match: dict) -> Iterator[tuple[str, str, str]]:
    for team_name, team_data in match.get("teams", {}).items():
        for player in team_data.get("roster", []):
            yield team_name, player["player_id"], player.get("nickname", "")


def _map_name(match: dict) -> str:
    voting = match.get("voting", {})
    if voting:
        picks = voting.get("map", {}).get("pick", [])
        if picks:
            return picks[0]
    return "unknown"


def _raw_to_id_teams(match: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for team_name, team_data in match.get("teams", {}).items():
        out[team_name] = [
            p["player_id"] for p in team_data.get("roster", []) if "player_id" in p
        ]
    return out


def _raw_to_score(match: dict) -> dict:
    return match.get("results", {}).get("score", {})


def _score_str(score: dict, your_team: str) -> str:
    if not score or not your_team or your_team == "unknown":
        return "N/A"
    your_score = score.get(your_team)
    if your_score is None:
        return "N/A"
    others = [v for k, v in score.items() if k != your_team]
    opp_score = others[0] if others else "?"
    return f"{your_score} / {opp_score}"


def _round_score_from_stats(stats: dict, series_score: dict) -> dict | None:
    try:
        stat_teams = stats["rounds"][0]["teams"]
        if len(stat_teams) != 2:
            return None
        scores = [int(t["team_stats"]["Final Score"]) for t in stat_teams]
        winner_faction = max(series_score, key=lambda f: series_score[f])
        loser_faction  = min(series_score, key=lambda f: series_score[f])
        winner_idx = 0 if scores[0] >= scores[1] else 1
        return {
            winner_faction: scores[winner_idx],
            loser_faction:  scores[1 - winner_idx],
        }
    except (KeyError, IndexError, ValueError, TypeError):
        return None


async def _fetch_player_history(client: FaceitClient, player_id: str, nickname: str = ""):
    """Returns (match_ids set, status_events list)."""
    match_ids: set[str] = set()
    events: list[dict] = []
    to_ts: int | None = None
    batch = 0
    while True:
        batch += 1
        window_ids: list[str] = []
        oldest_ts: int | None = None
        for page in range(MAX_PAGES):
            events.append({"type": "status", "msg": f"Fetching history for {nickname}… ({len(match_ids) + len(window_ids):,} so far)"})
            history = await client.get_match_history(
                player_id, game=GAME, offset=page * PAGE_SIZE, limit=PAGE_SIZE, to=to_ts
            )
            items = history.get("items", [])
            if not items:
                break
            window_ids.extend(item["match_id"] for item in items)
            oldest_ts = items[-1].get("started_at", oldest_ts)
            if len(items) < PAGE_SIZE:
                break
        match_ids.update(window_ids)
        log.info("History batch %d: %d new (total: %d)", batch, len(window_ids), len(match_ids))
        if len(window_ids) < PAGE_SIZE * MAX_PAGES or oldest_ts is None:
            break
        to_ts = oldest_ts - 1
    return match_ids, events


async def _fetch_single_match(client: FaceitClient, match_id: str) -> tuple[str, dict | None]:
    """Fetch a single match from API. Returns (match_id, raw_data | None)."""
    try:
        raw = await client.get_match(match_id)
        return match_id, raw
    except Exception as e:
        log.warning("Failed match %s: %s", match_id, e)
        return match_id, None


async def _process_raw_match(client: FaceitClient, match_id: str, raw: dict) -> dict:
    """Process a raw API match into a cached dict, saving to DB."""
    id_teams     = _raw_to_id_teams(raw)
    map_name     = _map_name(raw)
    started      = raw.get("started_at", 0)
    status       = raw.get("status", "unknown")
    url          = raw.get("faceit_url", "").replace("{lang}", "en")
    comp_type    = raw.get("competition_type", "") or ""
    comp_name    = raw.get("competition_name", "") or ""
    series_score = _raw_to_score(raw)

    raw_players = [(pid, nick) for _, pid, nick in _players_in_raw(raw) if nick]
    await upsert_players_bulk(raw_players)

    await save_match(match_id, started, status, map_name, id_teams, series_score,
                     url, comp_type, comp_name)
    for pid in (pid for _, pid, _ in _players_in_raw(raw)):
        await link_player_match(pid, match_id)

    return {
        "match_id": match_id, "started_at": started, "status": status,
        "map": map_name, "teams": id_teams, "score": series_score,
        "faceit_url": url, "competition_type": comp_type,
        "competition_name": comp_name, "_needs_stats": True,
    }


# ── streaming search ──────────────────────────────────────────────────────────

async def stream_search(
    client: FaceitClient,
    your_nickname: str,
    opponent_nickname: str,
) -> AsyncGenerator[dict, None]:
    await init_db()

    # ── Step 0: resolve nicknames → UUIDs ─────────────────────────────────────
    try:
        you_info = await client.get_player(your_nickname)
        opp_info = await client.get_player(opponent_nickname)
    except Exception as e:
        yield {"type": "error", "msg": str(e)}
        return

    your_id   = you_info["player_id"]
    your_nick = you_info["nickname"]
    opp_id    = opp_info["player_id"]
    opp_nick  = opp_info["nickname"]

    await upsert_player(your_id, your_nick)
    await upsert_player(opp_id, opp_nick)

    your_scanned = await is_player_fully_scanned(your_id)
    opp_scanned  = await is_player_fully_scanned(opp_id)

    # ── Step 1: history collection ────────────────────────────────────────────
    if your_scanned:
        your_match_ids = await get_player_match_ids(your_id)
        yield {"type": "status", "msg": f"Loaded {len(your_match_ids):,} matches for {your_nick} from cache"}
    else:
        your_match_ids, events = await _fetch_player_history(client, your_id, your_nick)
        for ev in events:
            yield ev
        await link_player_matches_bulk(your_id, list(your_match_ids))
        await mark_player_scanned(your_id, len(your_match_ids))
        yield {"type": "status", "msg": f"Got {len(your_match_ids):,} matches for {your_nick}"}

    if opp_scanned:
        opp_match_ids = await get_player_match_ids(opp_id)
        yield {"type": "status", "msg": f"Loaded {len(opp_match_ids):,} matches for {opp_nick} from cache"}
    else:
        opp_match_ids, events = await _fetch_player_history(client, opp_id, opp_nick)
        for ev in events:
            yield ev
        await link_player_matches_bulk(opp_id, list(opp_match_ids))
        await mark_player_scanned(opp_id, len(opp_match_ids))
        yield {"type": "status", "msg": f"Got {len(opp_match_ids):,} matches for {opp_nick}"}

    all_match_ids = list(your_match_ids | opp_match_ids)
    total = len(all_match_ids)
    yield {"type": "status", "msg": f"Checking {total:,} unique matches…"}
    yield {"type": "progress", "checked": 0, "total": total, "found": 0}

    # ── Step 2: scan matches in parallel batches ──────────────────────────────
    found_count = 0
    new_fetched = 0
    checked = 0

    for batch_start in range(0, total, BATCH_SIZE):
        batch_ids = all_match_ids[batch_start:batch_start + BATCH_SIZE]

        # Phase 1: check cache for entire batch
        cached_results: dict[str, dict | None] = {}
        to_fetch: list[str] = []
        for mid in batch_ids:
            c = await get_cached_match(mid)
            if c is not None:
                cached_results[mid] = c
            else:
                to_fetch.append(mid)

        # Phase 2: fetch uncached matches in parallel
        if to_fetch:
            fetch_tasks = [_fetch_single_match(client, mid) for mid in to_fetch]
            fetch_results = await asyncio.gather(*fetch_tasks)

            for mid, raw in fetch_results:
                if raw is None:
                    cached_results[mid] = None
                    continue
                processed = await _process_raw_match(client, mid, raw)
                cached_results[mid] = processed
                new_fetched += 1

        # Phase 3: check each match for shared players, yield results
        for mid in batch_ids:
            checked += 1
            cached = cached_results.get(mid)
            if cached is None:
                continue

            your_team = opp_team = None
            for faction, pids in cached["teams"].items():
                if your_id in pids: your_team = faction
                if opp_id  in pids: opp_team  = faction

            if your_team is None or opp_team is None:
                continue

            found_count += 1
            same_team = your_team == opp_team

            # Fetch real round score for confirmed shared matches
            score = cached["score"]
            if cached.get("_needs_stats"):
                try:
                    stats = await client.get_match_stats(mid)
                    round_score = _round_score_from_stats(stats, score)
                    if round_score:
                        score = round_score
                        await update_match_score(mid, score)
                except Exception as e:
                    log.debug("Could not fetch stats for %s: %s", mid, e)

            score_str = _score_str(score, your_team)

            all_pids = [pid for pids in cached["teams"].values() for pid in pids]
            nick_map = await get_nicknames(all_pids)
            display_teams = {
                faction: [nick_map.get(pid, pid[:8]) for pid in pids]
                for faction, pids in cached["teams"].items()
            }

            yield {
                "type": "match", "match_id": mid,
                "started_at": cached["started_at"], "status": cached["status"],
                "map": cached["map"], "teams": display_teams,
                "teams_ids": cached["teams"], "your_team": your_team,
                "opponent_team": opp_team, "score": score_str,
                "faceit_url": cached["faceit_url"],
                "competition_type": cached.get("competition_type", ""),
                "competition_name": cached.get("competition_name", ""),
                "same_team": same_team,
            }

        # Progress update after each batch
        yield {"type": "progress", "checked": checked, "total": total, "found": found_count}

    yield {
        "type": "done", "total_found": found_count,
        "total_checked": total, "new_fetched": new_fetched,
        "cached": your_scanned and opp_scanned,
    }
