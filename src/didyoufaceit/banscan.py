"""Ban scanner — find banned opponents in your match history.

Streams SSE events as it discovers banned players among opponents.
Paginated: fetches N matches at a time, user can request more.
Cancellation-aware: checks a stop event before each API call.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator

from .api import FaceitAPIError, FaceitClient
from .cache import (
    get_cached_ban_status,
    get_cached_matches_bulk,
    get_nicknames,
    init_db,
    save_ban_statuses_bulk,
    save_match,
    upsert_player,
    upsert_players_bulk,
)

log = logging.getLogger(__name__)

GAME = "cs2"
BAN_CHECK_BATCH = 10  # how many players to check bans for concurrently


# ── FACEIT ban fetching ──────────────────────────────────────────────────────


async def _fetch_player_bans(client: FaceitClient, player_id: str) -> list[dict]:
    """Fetch bans for a player from FACEIT API.  Returns list of ban objects."""
    try:
        data = await client._get(f"/players/{player_id}/bans", offset=0, limit=20)
        items = data.get("items", [])
        if items:
            log.info("Bans found for %s: %s", player_id, items)
        # Map actual FACEIT API fields:
        #   nickname, type, reason, starts_at, ends_at (optional), user_id
        #   No "expired" field — we infer from ends_at
        result = []
        for b in items:
            starts = b.get("starts_at", b.get("banStart", ""))
            ends = b.get("ends_at", b.get("banEnd", ""))
            result.append(
                {
                    "ban_id": b.get("banId", b.get("ban_id", b.get("user_id", ""))),
                    "nickname": b.get("nickname", ""),
                    "type": b.get("type", ""),
                    "reason": b.get("reason", ""),
                    "starts_at": starts,
                    "ends_at": ends,
                }
            )
        return result
    except FaceitAPIError as e:
        if e.status_code == 404:
            return []
        raise
    except Exception as e:
        log.warning("Failed to fetch bans for %s: %s", player_id, e)
        return []


# ── match helpers ────────────────────────────────────────────────────────────


def _opponents_from_match(match: dict, your_id: str) -> tuple[str | None, list[str]]:
    """Extract your team faction and opponent player IDs from a cached match.

    Returns (your_team_faction, [opponent_ids]).
    """
    your_team = None
    for faction, pids in match.get("teams", {}).items():
        if your_id in pids:
            your_team = faction
            break
    if your_team is None:
        return None, []
    opponents = []
    for faction, pids in match.get("teams", {}).items():
        if faction != your_team:
            opponents.extend(pids)
    return your_team, opponents


def _score_str(score: dict, your_team: str) -> str:
    """Format score from your perspective: 'X / Y'."""
    if not score or not your_team:
        return "N/A"
    your_score = score.get(your_team)
    if your_score is None:
        return "N/A"
    others = [v for k, v in score.items() if k != your_team]
    opp_score = others[0] if others else "?"
    return f"{your_score} / {opp_score}"


def _build_match_card(match: dict, your_id: str, nick_map: dict) -> dict:
    """Build a match card dict matching the format from stream_search."""
    your_team = None
    opp_team = None
    for faction, pids in match.get("teams", {}).items():
        if your_id in pids:
            your_team = faction
        else:
            opp_team = faction

    display_teams = {
        faction: [nick_map.get(pid, pid[:8]) for pid in pids] for faction, pids in match.get("teams", {}).items()
    }

    return {
        "match_id": match["match_id"],
        "started_at": match.get("started_at", 0),
        "status": match.get("status", ""),
        "map": match.get("map", "unknown"),
        "teams": display_teams,
        "teams_ids": match.get("teams", {}),
        "your_team": your_team or "",
        "opponent_team": opp_team or "",
        "score": _score_str(match.get("score", {}), your_team or ""),
        "faceit_url": match.get("faceit_url", ""),
        "competition_type": match.get("competition_type", ""),
        "competition_name": match.get("competition_name", ""),
    }


# ── main stream ──────────────────────────────────────────────────────────────


async def stream_ban_scan(
    client: FaceitClient,
    nickname: str,
    limit: int = 100,
    before_ts: int | None = None,
    stop_event: asyncio.Event | None = None,
) -> AsyncGenerator[dict, None]:
    """Stream ban scan results as SSE events.

    Pagination uses timestamps instead of offsets to bypass FACEIT's
    1000 offset limit. `before_ts` is the started_at of the oldest
    match from the previous page — we fetch matches older than that.
    """

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    await init_db()

    # ── Step 1: resolve player ────────────────────────────────────────────────
    yield {"type": "status", "msg": f"Resolving player {nickname}…"}
    if _stopped():
        return

    try:
        player = await client.get_player(nickname)
    except FaceitAPIError as e:
        yield {"type": "error", "msg": f"Player not found: {e}"}
        return

    player_id = player["player_id"]
    player_nick = player["nickname"]
    await upsert_player(player_id, player_nick)

    yield {"type": "player_resolved", "player_id": player_id, "nickname": player_nick}

    # ── Step 2: fetch match history page (timestamp-based) ────────────────────
    if before_ts:
        yield {"type": "status", "msg": f"Fetching next {limit} matches…"}
    else:
        yield {"type": "status", "msg": f"Fetching last {limit} matches…"}
    if _stopped():
        return

    # Use `to` parameter for timestamp pagination — offset always 0
    data = await client.get_match_history(
        player_id,
        game=GAME,
        offset=0,
        limit=limit,
        to=before_ts if before_ts else None,
    )
    raw_items = data.get("items", [])
    if not raw_items:
        yield {"type": "done", "total_checked": 0, "banned_count": 0, "has_more": False}
        return

    match_ids = [item["match_id"] for item in raw_items]
    total_matches = len(match_ids)

    # Track oldest match timestamp for next-page pagination
    oldest_ts = min(item.get("started_at", 0) for item in raw_items if item.get("started_at"))

    yield {"type": "status", "msg": f"Got {total_matches} matches, loading details…"}

    # ── Step 3: load match details (cache first, then API) ────────────────────
    if _stopped():
        return

    cached = await get_cached_matches_bulk(match_ids)
    to_fetch = [mid for mid in match_ids if cached.get(mid) is None]

    if to_fetch:
        yield {"type": "status", "msg": f"Fetching {len(to_fetch)} uncached match details…"}
        for i in range(0, len(to_fetch), BAN_CHECK_BATCH):
            if _stopped():
                return
            batch = to_fetch[i : i + BAN_CHECK_BATCH]
            tasks = [client.get_match(mid) for mid in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for mid, result in zip(batch, results):
                if isinstance(result, Exception):
                    log.warning("Failed to fetch match %s: %s", mid, result)
                    continue

                teams = {}
                for faction, fdata in result.get("teams", {}).items():
                    teams[faction] = [p["player_id"] for p in fdata.get("roster", []) if "player_id" in p]
                voting = result.get("voting", {})
                picks = voting.get("map", {}).get("pick", [])
                map_name = picks[0] if picks else "unknown"
                score = result.get("results", {}).get("score", {})

                await save_match(
                    mid,
                    result.get("started_at", 0),
                    result.get("status", "unknown"),
                    map_name,
                    teams,
                    score,
                    result.get("faceit_url", "").replace("{lang}", "en"),
                    result.get("competition_type", "") or "",
                    result.get("competition_name", "") or "",
                )
                cached[mid] = {
                    "match_id": mid,
                    "started_at": result.get("started_at", 0),
                    "status": result.get("status", "unknown"),
                    "map": map_name,
                    "teams": teams,
                    "score": score,
                    "faceit_url": result.get("faceit_url", "").replace("{lang}", "en"),
                    "competition_type": result.get("competition_type", "") or "",
                    "competition_name": result.get("competition_name", "") or "",
                }
                raw_players = []
                for fdata in result.get("teams", {}).values():
                    for p in fdata.get("roster", []):
                        if p.get("nickname"):
                            raw_players.append((p["player_id"], p["nickname"]))
                if raw_players:
                    await upsert_players_bulk(raw_players)

    # ── Step 4: collect unique opponents + their matches ──────────────────────
    if _stopped():
        return

    # player_id -> list of match dicts they appeared in as opponent
    opponent_matches: dict[str, list[dict]] = {}
    for mid in match_ids:
        m = cached.get(mid)
        if m is None:
            continue
        _, opps = _opponents_from_match(m, player_id)
        for opp_id in opps:
            if opp_id == player_id:
                continue
            if opp_id not in opponent_matches:
                opponent_matches[opp_id] = []
            opponent_matches[opp_id].append(m)

    unique_opponents = list(opponent_matches.keys())
    yield {
        "type": "status",
        "msg": f"Checking {len(unique_opponents)} unique opponents for bans…",
    }
    yield {
        "type": "progress",
        "total_matches": total_matches,
        "unique_opponents": len(unique_opponents),
        "checked": 0,
        "banned_found": 0,
    }

    # ── Step 5: check bans (cache first, then API) ───────────────────────────

    # Resolve all nicknames once for building match cards
    all_player_ids = list({pid for m in cached.values() if m for pids in m.get("teams", {}).values() for pid in pids})
    nick_map = await get_nicknames(all_player_ids)

    ban_cache = await get_cached_ban_status(unique_opponents)
    to_check = [pid for pid in unique_opponents if ban_cache[pid] is None]
    already_known = [pid for pid in unique_opponents if ban_cache[pid] is not None]

    checked_count = len(already_known)
    banned_count = 0

    # Emit already-known banned players immediately with full match cards
    for pid in already_known:
        info = ban_cache[pid]
        if info and info["is_banned"]:
            bans = info["bans"]
            if bans:
                banned_count += 1
                matches_with = opponent_matches.get(pid, [])
                match_cards = [_build_match_card(m, player_id, nick_map) for m in matches_with]
                yield {
                    "type": "banned_player",
                    "player_id": pid,
                    "nickname": nick_map.get(pid, pid[:8]),
                    "bans": bans,
                    "matches_against": len(matches_with),
                    "matches": match_cards,
                    "source": "cache",
                }

    yield {
        "type": "progress",
        "total_matches": total_matches,
        "unique_opponents": len(unique_opponents),
        "checked": checked_count,
        "banned_found": banned_count,
    }

    # Now check uncached/stale players in batches
    for i in range(0, len(to_check), BAN_CHECK_BATCH):
        if _stopped():
            yield {"type": "cancelled", "checked": checked_count, "banned_found": banned_count}
            return

        batch = to_check[i : i + BAN_CHECK_BATCH]
        tasks = [_fetch_player_bans(client, pid) for pid in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        bulk_save: list[tuple[str, bool, list[dict]]] = []

        for pid, bans_result in zip(batch, results):
            checked_count += 1
            if isinstance(bans_result, Exception):
                log.warning("Ban check failed for %s: %s", pid, bans_result)
                continue

            bans = bans_result
            # A player is "banned" if they have ANY ban records (active or expired)
            is_banned = len(bans) > 0

            bulk_save.append((pid, is_banned, bans))

            if is_banned:
                banned_count += 1
                matches_with = opponent_matches.get(pid, [])
                match_cards = [_build_match_card(m, player_id, nick_map) for m in matches_with]
                yield {
                    "type": "banned_player",
                    "player_id": pid,
                    "nickname": nick_map.get(pid, pid[:8]),
                    "bans": bans,
                    "matches_against": len(matches_with),
                    "matches": match_cards,
                    "source": "api",
                }

        if bulk_save:
            await save_ban_statuses_bulk(bulk_save)

        yield {
            "type": "progress",
            "total_matches": total_matches,
            "unique_opponents": len(unique_opponents),
            "checked": checked_count,
            "banned_found": banned_count,
        }

    # ── Done ──────────────────────────────────────────────────────────────────
    has_more = len(raw_items) >= limit
    # next page: fetch matches older than the oldest one we just saw
    next_before_ts = (oldest_ts - 1) if has_more and oldest_ts else None
    yield {
        "type": "done",
        "total_checked": checked_count,
        "banned_count": banned_count,
        "total_matches": total_matches,
        "has_more": has_more,
        "next_before_ts": next_before_ts,
    }
