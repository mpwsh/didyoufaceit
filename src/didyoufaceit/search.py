"""Search logic — yields SSE-style events as matches are discovered."""

from __future__ import annotations
import logging
from typing import Iterator, Generator

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


# ── helpers ───────────────────────────────────────────────────────────────────

def _players_in_raw(match: dict) -> Iterator[tuple[str, str, str]]:
    """Yields (faction, player_id, nickname) for every player in a raw match."""
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
    """Returns {faction: [player_id, ...]} — UUIDs only."""
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
    """
    Map real round scores back to faction names using the series result.
    series_score {"faction1": 1, "faction2": 0} tells us the winner.
    The stats team with more rounds = winning faction.
    """
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


def _fetch_player_history(client: FaceitClient, player_id: str, nickname: str = ""):
    """
    Generator — yields status events during fetch, then
    {"type": "_history_done", "match_ids": [...]} as the last event.
    """
    match_ids: set[str] = set()
    to_ts: int | None = None
    batch = 0
    while True:
        batch += 1
        window_ids: list[str] = []
        oldest_ts: int | None = None
        for page in range(MAX_PAGES):
            yield {"type": "status", "msg": f"Fetching history for {nickname}… ({len(match_ids) + len(window_ids):,} so far)"}
            history = client.get_match_history(
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
    yield {"type": "_history_done", "match_ids": list(match_ids)}


# ── streaming search ──────────────────────────────────────────────────────────

def stream_search(
    client: FaceitClient,
    your_nickname: str,
    opponent_nickname: str,
) -> Generator[dict, None, None]:
    """
    Generator that yields SSE event dicts:
      {"type": "status",   "msg": "..."}
      {"type": "progress", "checked": N, "total": N, "found": N}
      {"type": "match",    ...}
      {"type": "done",     ...}
      {"type": "error",    "msg": "..."}
    """
    init_db()

    # ── Step 0: resolve nicknames → UUIDs (always use latest profile) ─────────
    try:
        you_info = client.get_player(your_nickname)
        opp_info = client.get_player(opponent_nickname)
    except Exception as e:
        yield {"type": "error", "msg": str(e)}
        return

    your_id   = you_info["player_id"]
    your_nick = you_info["nickname"]   # canonical current nickname
    opp_id    = opp_info["player_id"]
    opp_nick  = opp_info["nickname"]

    # Store latest nicknames in players table
    upsert_player(your_id, your_nick)
    upsert_player(opp_id, opp_nick)

    your_scanned = is_player_fully_scanned(your_id)
    opp_scanned  = is_player_fully_scanned(opp_id)

    # ── Step 1: history collection ────────────────────────────────────────────
    if your_scanned:
        your_match_ids = get_player_match_ids(your_id)
        yield {"type": "status", "msg": f"Loaded {len(your_match_ids):,} matches for {your_nick} from cache"}
    else:
        your_match_ids = set()
        for ev in _fetch_player_history(client, your_id, your_nick):
            if ev["type"] == "_history_done":
                your_match_ids = set(ev["match_ids"])
            else:
                yield ev
        link_player_matches_bulk(your_id, list(your_match_ids))
        mark_player_scanned(your_id, len(your_match_ids))
        yield {"type": "status", "msg": f"Got {len(your_match_ids):,} matches for {your_nick}"}

    if opp_scanned:
        opp_match_ids = get_player_match_ids(opp_id)
        yield {"type": "status", "msg": f"Loaded {len(opp_match_ids):,} matches for {opp_nick} from cache"}
    else:
        opp_match_ids = set()
        for ev in _fetch_player_history(client, opp_id, opp_nick):
            if ev["type"] == "_history_done":
                opp_match_ids = set(ev["match_ids"])
            else:
                yield ev
        link_player_matches_bulk(opp_id, list(opp_match_ids))
        mark_player_scanned(opp_id, len(opp_match_ids))
        yield {"type": "status", "msg": f"Got {len(opp_match_ids):,} matches for {opp_nick}"}

    all_match_ids = list(your_match_ids | opp_match_ids)
    total = len(all_match_ids)
    yield {"type": "status", "msg": f"Checking {total:,} unique matches…"}
    yield {"type": "progress", "checked": 0, "total": total, "found": 0}

    # ── Step 2: scan each match ───────────────────────────────────────────────
    found_count = 0
    new_fetched = 0
    all_player_ids_seen: set[str] = set()  # for bulk nickname upsert at end

    for i, match_id in enumerate(all_match_ids):
        cached = get_cached_match(match_id)

        if cached is None:
            try:
                raw = client.get_match(match_id)
            except Exception as e:
                log.warning("Failed match %s: %s", match_id, e)
                if (i + 1) % 25 == 0:
                    yield {"type": "progress", "checked": i + 1, "total": total, "found": found_count}
                continue

            id_teams     = _raw_to_id_teams(raw)     # {faction: [player_id, ...]}
            map_name     = _map_name(raw)
            started      = raw.get("started_at", 0)
            status       = raw.get("status", "unknown")
            url          = raw.get("faceit_url", "").replace("{lang}", "en")
            comp_type    = raw.get("competition_type", "") or ""
            comp_name    = raw.get("competition_name", "") or ""
            series_score = _raw_to_score(raw)

            # Collect player id→nickname pairs to upsert into players table
            raw_players = [(pid, nick) for _, pid, nick in _players_in_raw(raw) if nick]
            upsert_players_bulk(raw_players)
            all_player_ids_seen.update(pid for _, pid, _ in _players_in_raw(raw))

            save_match(match_id, started, status, map_name, id_teams, series_score,
                       url, comp_type, comp_name)
            for pid in (pid for _, pid, _ in _players_in_raw(raw)):
                link_player_match(pid, match_id)

            cached = {
                "match_id": match_id, "started_at": started, "status": status,
                "map": map_name, "teams": id_teams, "score": series_score,
                "faceit_url": url, "competition_type": comp_type, "competition_name": comp_name,
                "_needs_stats": True,
            }
            new_fetched += 1

        # ── Determine which faction each player is on (by UUID) ───────────────
        your_team = opp_team = None
        for faction, pids in cached["teams"].items():
            if your_id in pids: your_team = faction
            if opp_id  in pids: opp_team  = faction

        if your_team is None or opp_team is None:
            if (i + 1) % 25 == 0:
                yield {"type": "progress", "checked": i + 1, "total": total, "found": found_count}
            continue

        found_count += 1
        same_team = your_team == opp_team

        # Fetch real round score only for confirmed shared matches
        score = cached["score"]
        if cached.get("_needs_stats"):
            try:
                stats = client.get_match_stats(match_id)
                round_score = _round_score_from_stats(stats, score)
                if round_score:
                    score = round_score
                    update_match_score(match_id, score)
            except Exception as e:
                log.debug("Could not fetch stats for %s: %s", match_id, e)

        score_str = _score_str(score, your_team)

        # Resolve display nicknames for this match's roster from players table
        all_pids = [pid for pids in cached["teams"].values() for pid in pids]
        nick_map = get_nicknames(all_pids)
        display_teams = {
            faction: [nick_map.get(pid, pid[:8]) for pid in pids]
            for faction, pids in cached["teams"].items()
        }

        yield {
            "type":             "match",
            "match_id":         match_id,
            "started_at":       cached["started_at"],
            "status":           cached["status"],
            "map":              cached["map"],
            "teams":            display_teams,        # nicknames for display
            "teams_ids":        cached["teams"],      # UUIDs for identity
            "your_team":        your_team,
            "opponent_team":    opp_team,
            "score":            score_str,
            "faceit_url":       cached["faceit_url"],
            "competition_type": cached.get("competition_type", ""),
            "competition_name": cached.get("competition_name", ""),
            "same_team":        same_team,
        }

        if (i + 1) % 25 == 0:
            yield {"type": "progress", "checked": i + 1, "total": total, "found": found_count}

    yield {
        "type":          "done",
        "total_found":   found_count,
        "total_checked": total,
        "new_fetched":   new_fetched,
        "cached":        your_scanned and opp_scanned,
    }