"""Async search logic — yields SSE-style events as matches are discovered."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Iterator

from .api import FaceitClient
from .cache import (
    get_cached_match,
    get_nicknames,
    get_player_match_ids,
    get_player_scan_info,
    init_db,
    link_player_match,
    link_player_matches_bulk,
    mark_player_scanned,
    save_match,
    update_match_score,
    upsert_player,
    upsert_players_bulk,
)

log = logging.getLogger(__name__)

GAME = "cs2"
PAGE_SIZE = 100
MAX_PAGES = 10
BATCH_SIZE = 10
SCAN_TTL_SECONDS = 6 * 3600  # Re-fetch recent history if last scan > 6 hours ago
RECENT_PAGES = 3  # Only fetch last ~300 matches for incremental refresh  # parallel match fetches per batch


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
        out[team_name] = [p["player_id"] for p in team_data.get("roster", []) if "player_id" in p]
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
        loser_faction = min(series_score, key=lambda f: series_score[f])
        winner_idx = 0 if scores[0] >= scores[1] else 1
        return {
            winner_faction: scores[winner_idx],
            loser_faction: scores[1 - winner_idx],
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
            count = len(match_ids) + len(window_ids)
            events.append({"type": "status", "msg": f"Fetching history for {nickname}… ({count:,} so far)"})
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


async def _fetch_recent_history(client: FaceitClient, player_id: str, nickname: str = ""):
    """Fetch only the most recent matches (for incremental refresh)."""
    match_ids: set[str] = set()
    events: list[dict] = []
    for page in range(RECENT_PAGES):
        events.append({"type": "status", "msg": f"Checking recent matches for {nickname}… ({len(match_ids)} found)"})
        history = await client.get_match_history(
            player_id,
            game=GAME,
            offset=page * PAGE_SIZE,
            limit=PAGE_SIZE,
        )
        items = history.get("items", [])
        if not items:
            break
        match_ids.update(item["match_id"] for item in items)
        if len(items) < PAGE_SIZE:
            break
    return match_ids, events


async def _collect_player_history(client, player_id, nickname):
    """
    Smart history collection:
    - Never scanned → full scan
    - Scanned recently (< SCAN_TTL) → use cache only
    - Scanned but stale (> SCAN_TTL) → cached + fetch recent matches
    Returns (match_ids, events, was_cached)
    """
    import time

    scan_info = await get_player_scan_info(player_id)

    if scan_info is None:
        # Never scanned — full scan
        match_ids, events = await _fetch_player_history(client, player_id, nickname)
        await link_player_matches_bulk(player_id, list(match_ids))
        await mark_player_scanned(player_id, len(match_ids))
        events.append({"type": "status", "msg": f"Got {len(match_ids):,} matches for {nickname}"})
        return match_ids, events, False

    age = int(time.time()) - (scan_info["scanned_at"] or 0)
    cached_ids = await get_player_match_ids(player_id)

    if age < SCAN_TTL_SECONDS:
        # Fresh — use cache
        msg = f"Loaded {len(cached_ids):,} matches for {nickname} from cache"
        return cached_ids, [{"type": "status", "msg": msg}], True

    # Stale — incremental refresh (fetch recent ~300 matches + merge)
    recent_ids, events = await _fetch_recent_history(client, player_id, nickname)
    new_ids = recent_ids - cached_ids
    if new_ids:
        await link_player_matches_bulk(player_id, list(new_ids))
        events.append({"type": "status", "msg": f"Found {len(new_ids)} new matches for {nickname}"})
    all_ids = cached_ids | recent_ids
    await mark_player_scanned(player_id, len(all_ids))
    events.append({"type": "status", "msg": f"Total {len(all_ids):,} matches for {nickname}"})
    return all_ids, events, len(new_ids) == 0


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
    id_teams = _raw_to_id_teams(raw)
    map_name = _map_name(raw)
    started = raw.get("started_at", 0)
    status = raw.get("status", "unknown")
    url = raw.get("faceit_url", "").replace("{lang}", "en")
    comp_type = raw.get("competition_type", "") or ""
    comp_name = raw.get("competition_name", "") or ""
    series_score = _raw_to_score(raw)

    raw_players = [(pid, nick) for _, pid, nick in _players_in_raw(raw) if nick]
    await upsert_players_bulk(raw_players)

    await save_match(match_id, started, status, map_name, id_teams, series_score, url, comp_type, comp_name)
    for pid in (pid for _, pid, _ in _players_in_raw(raw)):
        await link_player_match(pid, match_id)

    return {
        "match_id": match_id,
        "started_at": started,
        "status": status,
        "map": map_name,
        "teams": id_teams,
        "score": series_score,
        "faceit_url": url,
        "competition_type": comp_type,
        "competition_name": comp_name,
        "_needs_stats": True,
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

    your_id = you_info["player_id"]
    your_nick = you_info["nickname"]
    opp_id = opp_info["player_id"]
    opp_nick = opp_info["nickname"]

    await upsert_player(your_id, your_nick, you_info.get("steam_id_64", ""), you_info.get("avatar", ""))
    await upsert_player(opp_id, opp_nick, opp_info.get("steam_id_64", ""), opp_info.get("avatar", ""))

    # ── Step 1: history collection (smart: full scan / cache / incremental) ──
    your_match_ids, your_events, your_cached = await _collect_player_history(client, your_id, your_nick)
    for ev in your_events:
        yield ev

    opp_match_ids, opp_events, opp_cached = await _collect_player_history(client, opp_id, opp_nick)
    for ev in opp_events:
        yield ev

    all_match_ids = list(your_match_ids | opp_match_ids)
    total = len(all_match_ids)
    yield {"type": "status", "msg": f"Checking {total:,} unique matches…"}
    yield {"type": "progress", "checked": 0, "total": total, "found": 0}

    # ── Step 2: scan matches in parallel batches ──────────────────────────────
    found_count = 0
    new_fetched = 0
    checked = 0

    for batch_start in range(0, total, BATCH_SIZE):
        batch_ids = all_match_ids[batch_start : batch_start + BATCH_SIZE]

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
                if your_id in pids:
                    your_team = faction
                if opp_id in pids:
                    opp_team = faction

            if your_team is None or opp_team is None:
                continue

            found_count += 1
            same_team = your_team == opp_team

            # Fetch real round score if we only have series scores (0/1, 1/0)
            score = cached["score"]
            score_vals = [v for v in score.values() if isinstance(v, int | float)]
            needs_real_score = cached.get("_needs_stats") or (score_vals and max(score_vals) <= 1)

            if needs_real_score:
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
                faction: [nick_map.get(pid, pid[:8]) for pid in pids] for faction, pids in cached["teams"].items()
            }

            yield {
                "type": "match",
                "match_id": mid,
                "started_at": cached["started_at"],
                "status": cached["status"],
                "map": cached["map"],
                "teams": display_teams,
                "teams_ids": cached["teams"],
                "your_team": your_team,
                "opponent_team": opp_team,
                "score": score_str,
                "faceit_url": cached["faceit_url"],
                "competition_type": cached.get("competition_type", ""),
                "competition_name": cached.get("competition_name", ""),
                "same_team": same_team,
            }

        # Progress update after each batch
        yield {"type": "progress", "checked": checked, "total": total, "found": found_count}

    yield {
        "type": "done",
        "total_found": found_count,
        "total_checked": total,
        "new_fetched": new_fetched,
        "cached": your_cached and opp_cached,
    }
