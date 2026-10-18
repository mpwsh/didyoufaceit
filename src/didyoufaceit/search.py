"""Head-to-head search: every match two players shared, streamed as SSE events.

1. Walk both players' match history (cached per player; a page of 100 per call). History
   items carry rosters, so every match is stored as it is read, with no per-match calls.
2. Shared matches are the intersection of the two histories.
3. Only those get one /matches/{id}/stats call each for the map and round score.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator

from .api import FaceitAPIError, FaceitClient
from .cache import (
    get_nicknames,
    get_player_match_ids,
    get_player_scan_info,
    link_player_matches_bulk,
    mark_player_scanned,
    upsert_player,
)
from .matches import load_matches, score_str, store_history_items

log = logging.getLogger(__name__)

GAME = "cs2"
PAGE_SIZE = 100
MAX_PAGES = 10  # FACEIT's offset cap is 1,000, so walk windows of 10 pages then move `to` back
SCAN_TTL_SECONDS = 6 * 3600  # re-check recent history if the last scan is older than this
RESULT_BATCH = 25  # shared matches resolved (and streamed) per batch


class _Ids:
    """Final item of a history walk: the match ids, distinguishable from status events."""

    def __init__(self, ids: set[str], cached: bool):
        self.ids = ids
        self.cached = cached


async def _walk_history(
    client: FaceitClient, player_id: str, nickname: str, *, known: set[str] | None = None
) -> AsyncGenerator[dict | set[str], None]:
    """Yield a status event per page, then the set of match ids read. Each page is stored as read.

    With `known` (a previous scan), stop at the first page that holds nothing new: history is
    newest first, so everything after it was read last time. A player who played more than a
    page since then keeps paging until the gap is closed.
    """
    seen: set[str] = set()
    to_ts: int | None = None
    while True:
        window_new = 0
        oldest: int | None = None
        for page in range(MAX_PAGES):
            yield {"type": "status", "msg": f"Fetching history for {nickname}… ({len(seen):,} so far)"}
            history = await client.get_match_history(
                player_id, game=GAME, offset=page * PAGE_SIZE, limit=PAGE_SIZE, to=to_ts
            )
            items = history.get("items") or []
            if not items:
                break
            ids = await store_history_items(items)
            await link_player_matches_bulk(player_id, ids)
            new = set(ids) - seen
            window_new += len(new)
            seen.update(ids)
            starts = [i["started_at"] for i in items if i.get("started_at")]
            if starts:
                oldest = min(starts) if oldest is None else min(oldest, *starts)
            if known is not None and not (new - known):
                break  # caught up with the previous scan
            if len(items) < PAGE_SIZE:
                break
        else:
            # A full window of MAX_PAGES (FACEIT's offset cap): continue before the oldest match seen
            if window_new and oldest:
                to_ts = oldest - 1
                continue
        break
    yield seen


async def _collect_player_history(
    client: FaceitClient, player_id: str, nickname: str
) -> AsyncGenerator[dict | _Ids, None]:
    """Never scanned → full walk; scanned within SCAN_TTL → cache only; older → new pages + cache."""
    scan_info = await get_player_scan_info(player_id)
    if scan_info and time.time() - (scan_info["scanned_at"] or 0) < SCAN_TTL_SECONDS:
        ids = await get_player_match_ids(player_id)
        yield {"type": "status", "msg": f"Loaded {len(ids):,} matches for {nickname} from cache"}
        yield _Ids(ids, cached=True)
        return

    cached_ids = await get_player_match_ids(player_id) if scan_info else set()
    walked: set[str] = set()
    async for ev in _walk_history(client, player_id, nickname, known=cached_ids if scan_info else None):
        if isinstance(ev, set):
            walked = ev
        else:
            yield ev
    all_ids = cached_ids | walked
    # Marked only once the walk finished: an interrupted scan is redone, and its pages stay linked
    await mark_player_scanned(player_id, len(all_ids))
    new = len(walked - cached_ids)
    msg = f"Got {len(all_ids):,} matches for {nickname}" + (f" ({new:,} new)" if scan_info else "")
    yield {"type": "status", "msg": msg}
    yield _Ids(all_ids, cached=bool(scan_info) and new == 0)


async def _resolve(client: FaceitClient, nickname: str) -> dict:
    info = await client.get_player(nickname)
    await upsert_player(info["player_id"], info["nickname"], info.get("steam_id_64", ""), info.get("avatar", ""))
    return info


async def stream_search(
    client: FaceitClient,
    your_nickname: str,
    opponent_nickname: str,
) -> AsyncGenerator[dict, None]:
    # ── Step 0: resolve nicknames → UUIDs ─────────────────────────────────────
    players = []
    for nick in (your_nickname, opponent_nickname):
        try:
            players.append(await _resolve(client, nick))
        except FaceitAPIError as e:
            if e.status_code == 404:
                yield {"type": "error", "status": 404, "msg": f"Player {nick!r} not found"}
                return
            raise
    you, opp = players
    your_id, opp_id = you["player_id"], opp["player_id"]
    if your_id == opp_id:
        yield {"type": "error", "status": 400, "msg": "Pick two different players"}
        return
    yield {
        "type": "players_resolved",
        "you": {"player_id": your_id, "nickname": you["nickname"]},
        "opponent": {"player_id": opp_id, "nickname": opp["nickname"]},
    }

    # ── Step 1: both histories, streaming progress page by page ───────────────
    histories: list[_Ids] = []
    for pid, nick in ((your_id, you["nickname"]), (opp_id, opp["nickname"])):
        async for ev in _collect_player_history(client, pid, nick):
            if isinstance(ev, _Ids):
                histories.append(ev)
            else:
                yield ev

    # ── Step 2: only matches both played can match ────────────────────────────
    shared = sorted(histories[0].ids & histories[1].ids)
    total = len(shared)
    yield {"type": "status", "msg": f"{total:,} shared matches, loading details…"}
    yield {"type": "progress", "checked": 0, "total": total, "found": 0}

    found = fetched = checked = 0
    for start in range(0, total, RESULT_BATCH):
        batch = shared[start : start + RESULT_BATCH]
        before = {"fetched": 0}
        rows = await load_matches(client, batch, with_details=True, counter=before)
        fetched += before["fetched"]
        nick_map = await get_nicknames([pid for m in rows.values() for pids in m["teams"].values() for pid in pids])

        for mid in batch:
            checked += 1
            m = rows.get(mid)
            if m is None:
                continue
            your_team = next((f for f, pids in m["teams"].items() if your_id in pids), None)
            opp_team = next((f for f, pids in m["teams"].items() if opp_id in pids), None)
            if your_team is None or opp_team is None:
                continue
            found += 1
            yield {
                "type": "match",
                "match_id": mid,
                "started_at": m["started_at"],
                "status": m["status"],
                "map": m["map"],
                "teams": {f: [nick_map.get(pid, pid[:8]) for pid in pids] for f, pids in m["teams"].items()},
                "teams_ids": m["teams"],
                "your_team": your_team,
                "opponent_team": opp_team,
                "score": score_str(m["score"], your_team),
                "faceit_url": m["faceit_url"],
                "competition_type": m["competition_type"],
                "competition_name": m["competition_name"],
                "same_team": your_team == opp_team,
            }
        yield {"type": "progress", "checked": checked, "total": total, "found": found}

    yield {
        "type": "done",
        "total_found": found,
        "total_checked": total,
        "new_fetched": fetched,
        "cached": all(h.cached for h in histories),
    }
