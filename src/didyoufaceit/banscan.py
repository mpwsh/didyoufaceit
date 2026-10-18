"""Ban scanner: find banned opponents in a player's match history, streamed as SSE events.

One history page (up to 100 matches) gives every opponent with no per-match calls; only the
matches against banned players get a /stats call for map and score. Paginated by timestamp
(`before`), so it isn't limited by FACEIT's 1,000 offset cap.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator

from .api import FaceitAPIError, FaceitClient
from .cache import (
    get_cached_ban_status,
    get_players,
    save_ban_statuses_bulk,
    upsert_player,
)
from .matches import load_matches, score_str, store_history_items

log = logging.getLogger(__name__)

GAME = "cs2"
BAN_CHECK_BATCH = 10  # ban lookups per batch (the client's semaphore caps concurrency further)


def _normalize_ban(b: dict) -> dict:
    return {
        "ban_id": b.get("banId") or b.get("ban_id") or "",
        "nickname": b.get("nickname", ""),
        "type": b.get("type", ""),
        "reason": b.get("reason", ""),
        "starts_at": b.get("starts_at") or b.get("banStart") or "",
        "ends_at": b.get("ends_at") or b.get("banEnd") or "",
    }


def _match_card(match: dict, your_id: str, nicks: dict[str, str]) -> dict:
    your_team = next((f for f, pids in match["teams"].items() if your_id in pids), None)
    opp_team = next((f for f in match["teams"] if f != your_team), None)
    return {
        "match_id": match["match_id"],
        "started_at": match.get("started_at", 0),
        "status": match.get("status", ""),
        "map": match.get("map", "unknown"),
        "teams": {f: [nicks.get(pid, pid[:8]) for pid in pids] for f, pids in match["teams"].items()},
        "teams_ids": match["teams"],
        "your_team": your_team or "",
        "opponent_team": opp_team or "",
        "score": score_str(match.get("score", {}), your_team),
        "faceit_url": match.get("faceit_url", ""),
        "competition_type": match.get("competition_type", ""),
        "competition_name": match.get("competition_name", ""),
    }


async def stream_ban_scan(
    client: FaceitClient,
    nickname: str,
    limit: int = 100,
    before_ts: int | None = None,
) -> AsyncGenerator[dict, None]:
    # ── Step 1: resolve player ────────────────────────────────────────────────
    yield {"type": "status", "msg": f"Resolving player {nickname}…"}
    try:
        player = await client.get_player(nickname)
    except FaceitAPIError as e:
        if e.status_code == 404:
            yield {"type": "error", "status": 404, "msg": f"Player {nickname!r} not found"}
            return
        raise
    player_id = player["player_id"]
    await upsert_player(player_id, player["nickname"], player.get("steam_id_64", ""), player.get("avatar", ""))
    yield {"type": "player_resolved", "player_id": player_id, "nickname": player["nickname"]}

    # ── Step 2: one history page; rosters come with it ────────────────────────
    yield {"type": "status", "msg": f"Fetching {'next' if before_ts else 'last'} {limit} matches…"}
    data = await client.get_match_history(player_id, game=GAME, offset=0, limit=limit, to=before_ts)
    items = data.get("items") or []
    if not items:
        yield {"type": "done", "total_checked": 0, "banned_count": 0, "total_matches": 0, "has_more": False}
        return
    match_ids = await store_history_items(items)
    oldest_ts = min((i["started_at"] for i in items if i.get("started_at")), default=None)
    rows = await load_matches(client, match_ids, with_details=False)

    # ── Step 3: opponents and the matches you played against each ─────────────
    opponent_matches: dict[str, list[dict]] = {}
    for mid in match_ids:
        m = rows.get(mid)
        if not m:
            continue
        your_team = next((f for f, pids in m["teams"].items() if player_id in pids), None)
        if your_team is None:
            continue
        for faction, pids in m["teams"].items():
            if faction != your_team:
                for opp in pids:
                    opponent_matches.setdefault(opp, []).append(m)
    opponents = list(opponent_matches)
    total_matches = len(match_ids)
    yield {"type": "status", "msg": f"Checking {len(opponents)} unique opponents for bans…"}

    profiles = await get_players([pid for m in rows.values() for pids in m["teams"].values() for pid in pids])
    nicks = {pid: p["nickname"] for pid, p in profiles.items()}

    async def banned_event(pid: str, bans: list[dict], source: str) -> dict:
        matches_with = opponent_matches.get(pid, [])
        detailed = await load_matches(client, [m["match_id"] for m in matches_with], with_details=True)
        matches_with = [detailed.get(m["match_id"], m) for m in matches_with]
        prof = profiles.get(pid) or {}
        if not prof.get("steam_id_64"):
            try:  # history rosters normally carry the Steam id; ask FACEIT only when they didn't
                full = await client.get_player_by_id(pid)
                prof = {"steam_id_64": full.get("steam_id_64", ""), "avatar": full.get("avatar", "")}
                await upsert_player(pid, full.get("nickname") or nicks.get(pid, pid[:8]), **prof)
            except FaceitAPIError as e:
                log.debug("No profile for %s: %s", pid, e)
        return {
            "type": "banned_player",
            "player_id": pid,
            "nickname": nicks.get(pid, pid[:8]),
            "steam_id_64": prof.get("steam_id_64", ""),
            "avatar": prof.get("avatar", ""),
            "bans": bans,
            "matches_against": len(matches_with),
            "matches": [_match_card(m, player_id, nicks) for m in matches_with],
            "source": source,
        }

    # ── Step 4: bans, cache first ─────────────────────────────────────────────
    ban_cache = await get_cached_ban_status(opponents)
    to_check = [pid for pid in opponents if ban_cache[pid] is None]
    checked = len(opponents) - len(to_check)
    banned = failed = 0
    for pid in opponents:
        info = ban_cache[pid]
        if info and info["is_banned"] and info["bans"]:
            banned += 1
            yield await banned_event(pid, info["bans"], "cache")

    def progress() -> dict:
        return {
            "type": "progress",
            "total_matches": total_matches,
            "unique_opponents": len(opponents),
            "checked": checked,
            "failed": failed,
            "banned_found": banned,
        }

    yield progress()
    for i in range(0, len(to_check), BAN_CHECK_BATCH):
        batch = to_check[i : i + BAN_CHECK_BATCH]
        results = await asyncio.gather(*(client.get_player_bans(pid) for pid in batch), return_exceptions=True)
        to_save: list[tuple[str, bool, list[dict]]] = []
        for pid, result in zip(batch, results):
            if isinstance(result, BaseException):
                # Not cached: a timeout must not mark a player clean for a week
                log.warning("Ban check failed for %s: %s", pid, result)
                failed += 1
                continue
            checked += 1
            bans = [_normalize_ban(b) for b in result]
            to_save.append((pid, bool(bans), bans))
            if bans:
                banned += 1
                yield await banned_event(pid, bans, "api")
        await save_ban_statuses_bulk(to_save)
        yield progress()

    # ── Done ──────────────────────────────────────────────────────────────────
    has_more = len(items) >= limit
    yield {
        "type": "done",
        "total_checked": checked,
        "failed_checks": failed,
        "banned_count": banned,
        "total_matches": total_matches,
        "has_more": has_more,
        "next_before_ts": (oldest_ts - 1) if has_more and oldest_ts else None,
    }
