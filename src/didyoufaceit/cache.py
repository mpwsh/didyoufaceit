"""Async PostgreSQL cache for FACEIT match data and player scan state.

Bulk operations use unnest() so a page of 100 matches is one round trip.
Schema setup runs once per process start, serialized across workers by an advisory lock;
the TTL cleanup runs in whichever worker grabs its lock first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime

import asyncpg

log = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "postgresql://didyou:didyou@localhost:5432/didyou"

# ── TTL configuration ─────────────────────────────────────────────────────────

STATS_TTL_SECONDS = 3 * 24 * 3600  # 3 days  — player lifetime stats
RECENT_TTL_SECONDS = 6 * 3600  # 6 hours — player's recent matches (current form)
MATCHROOM_TTL_SECONDS = 7 * 24 * 3600  # 7 days  — matchroom snapshots
MATCH_TTL_SECONDS = 365 * 24 * 3600  # 1 year  — match rows; re-fetched on demand after that

# Ban cache — per-player adaptive TTL:
#   Clean player, or every ban already over → re-check after 7 days
#   Active ban                               → re-check after it ends (+ small buffer)
#   Permanent ban                            → re-check every 90 days
BAN_CLEAN_TTL_SECONDS = 7 * 24 * 3600
BAN_PERMANENT_TTL_SECONDS = 90 * 24 * 3600
BAN_EXPIRY_BUFFER_SECONDS = 6 * 3600

CLEANUP_INTERVAL_SECONDS = 6 * 3600

FINAL_MATCH_STATUSES = {"FINISHED", "CANCELLED", "ABORTED"}
# /stats may 404 until a few minutes after a match ends; only older 404s mean "never"
STATS_MISSING_AFTER_SECONDS = 24 * 3600

# Advisory lock keys (any constant bigint)
_SCHEMA_LOCK = 7_301_001
_CLEANUP_LOCK = 7_301_002

# ── pool configuration ────────────────────────────────────────────────────────

POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5
POOL_COMMAND_TIMEOUT = 15
POOL_MAX_INACTIVE_CONNECTION_LIFETIME = 300

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()
_cleanup_task: asyncio.Task | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),  # read late so .env loading order doesn't matter
                min_size=POOL_MIN_SIZE,
                max_size=POOL_MAX_SIZE,
                command_timeout=POOL_COMMAND_TIMEOUT,
                max_inactive_connection_lifetime=POOL_MAX_INACTIVE_CONNECTION_LIFETIME,
            )
    return _pool


# ── lifecycle ─────────────────────────────────────────────────────────────────

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS players (
        player_id    TEXT PRIMARY KEY,
        nickname     TEXT NOT NULL,
        steam_id_64  TEXT DEFAULT '',
        avatar       TEXT DEFAULT '',
        updated_at   INT DEFAULT (EXTRACT(EPOCH FROM NOW())::int)
    );

    CREATE TABLE IF NOT EXISTS matches (
        match_id         TEXT PRIMARY KEY,
        started_at       INT,
        status           TEXT,
        map              TEXT,
        teams_json       JSONB,
        score_json       JSONB,
        faceit_url       TEXT,
        competition_type TEXT DEFAULT '',
        competition_name TEXT DEFAULT '',
        fetched_at       INT DEFAULT (EXTRACT(EPOCH FROM NOW())::int)
    );

    CREATE TABLE IF NOT EXISTS player_matches (
        player_id   TEXT NOT NULL,
        match_id    TEXT NOT NULL,
        PRIMARY KEY (player_id, match_id)
    );

    CREATE TABLE IF NOT EXISTS player_scans (
        player_id       TEXT PRIMARY KEY,
        scanned_at      INT DEFAULT (EXTRACT(EPOCH FROM NOW())::int),
        total_matches   INT DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS player_stats_cache (
        player_id    TEXT PRIMARY KEY,
        stats_json   JSONB NOT NULL,
        fetched_at   INT DEFAULT (EXTRACT(EPOCH FROM NOW())::int)
    );

    CREATE TABLE IF NOT EXISTS player_recent_cache (
        player_id    TEXT PRIMARY KEY,
        rows_json    JSONB NOT NULL,
        fetched_at   INT DEFAULT (EXTRACT(EPOCH FROM NOW())::int)
    );

    CREATE TABLE IF NOT EXISTS matchroom_cache (
        match_id      TEXT PRIMARY KEY,
        data_json     JSONB NOT NULL,
        fetched_at    INT DEFAULT (EXTRACT(EPOCH FROM NOW())::int)
    );

    CREATE TABLE IF NOT EXISTS player_bans (
        player_id    TEXT PRIMARY KEY,
        is_banned    BOOLEAN NOT NULL DEFAULT FALSE,
        bans_json    JSONB DEFAULT '[]'::jsonb,
        checked_at   INT DEFAULT (EXTRACT(EPOCH FROM NOW())::int),
        recheck_at   INT DEFAULT (EXTRACT(EPOCH FROM NOW())::int)
    );

    CREATE TABLE IF NOT EXISTS schema_migrations (
        name        TEXT PRIMARY KEY,
        applied_at  INT DEFAULT (EXTRACT(EPOCH FROM NOW())::int)
    );

    ALTER TABLE players ADD COLUMN IF NOT EXISTS steam_id_64 TEXT DEFAULT '';
    ALTER TABLE players ADD COLUMN IF NOT EXISTS avatar TEXT DEFAULT '';
    -- TRUE once map and round score came from /matches/{id}/stats (or it was tried and failed)
    ALTER TABLE matches ADD COLUMN IF NOT EXISTS stats_fetched BOOLEAN NOT NULL DEFAULT FALSE;

    CREATE INDEX IF NOT EXISTS idx_player_matches_match  ON player_matches(match_id);
    CREATE INDEX IF NOT EXISTS idx_matches_fetched       ON matches(fetched_at);
    CREATE INDEX IF NOT EXISTS idx_stats_fetched         ON player_stats_cache(fetched_at);
    CREATE INDEX IF NOT EXISTS idx_recent_fetched        ON player_recent_cache(fetched_at);
    CREATE INDEX IF NOT EXISTS idx_matchroom_fetched     ON matchroom_cache(fetched_at);
    CREATE INDEX IF NOT EXISTS idx_bans_recheck          ON player_bans(recheck_at);
"""

# One-off data fixes, applied once per database in this order.
_MIGRATIONS: list[tuple[str, str]] = [
    (
        # Cleanup used to delete player_matches links whose match row was gone, truncating
        # search history while player_scans said the player was fully scanned. Rescan everyone.
        "2026_10_rescan_truncated_histories",
        "DELETE FROM player_scans",
    ),
    (
        # Rows saved before stats_fetched existed: trust those that already have a map and a
        # round score (anything above 1), re-check the rest.
        "2026_10_mark_existing_match_details",
        r"""
        UPDATE matches SET stats_fetched = TRUE
        WHERE map IS NOT NULL AND map <> 'unknown'
          AND jsonb_typeof(score_json) = 'object'
          AND EXISTS (
              SELECT 1 FROM jsonb_each_text(score_json) v
              WHERE v.value ~ '^\d+$' AND v.value::int > 1
          )
        """,
    ),
    (
        # The old code cached rooms mid-veto for 7 days, so the picked map never updated.
        "2026_10_drop_live_matchroom_snapshots",
        """
        DELETE FROM matchroom_cache
        WHERE upper(COALESCE(data_json->>'status', '')) NOT IN ('FINISHED', 'CANCELLED', 'ABORTED')
        """,
    ),
]


async def init_db() -> None:
    """Create or upgrade the schema (once, across all workers) and start the cleanup loop."""
    global _cleanup_task
    # A dedicated connection without the pool's 15 s command timeout: a migration over a large
    # matches table, or a worker waiting for another worker's migration, can take longer.
    conn = await asyncpg.connect(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", _SCHEMA_LOCK)
        try:
            await conn.execute(_SCHEMA)
            for name, sql in _MIGRATIONS:
                async with conn.transaction():
                    done = await conn.fetchval("SELECT 1 FROM schema_migrations WHERE name = $1", name)
                    if done:
                        continue
                    log.info("Applying migration %s…", name)
                    await conn.execute(sql)
                    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
                    log.info("Migration applied: %s", name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _SCHEMA_LOCK)
    finally:
        await conn.close()
    await _get_pool()
    log.info("Database schema ready")

    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_loop())


async def close_db() -> None:
    """Cancel cleanup and close the connection pool."""
    global _pool, _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None
    if _pool:
        await _pool.close()
        _pool = None


# ── background TTL cleanup ────────────────────────────────────────────────────


async def _cleanup_loop() -> None:
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            await cleanup_expired()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Cache cleanup error (will retry next cycle)")


async def cleanup_expired() -> bool:
    """Delete rows past their TTL. Returns False if another worker is already cleaning.

    player_matches (each player's history index) and players are never pruned: the index is
    what makes a cached search complete, and both tables are small per row.
    """
    now = int(time.time())
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", _CLEANUP_LOCK):
            return False
        try:
            results = {
                "stats": await conn.execute(
                    "DELETE FROM player_stats_cache WHERE fetched_at < $1", now - STATS_TTL_SECONDS
                ),
                "recent": await conn.execute(
                    "DELETE FROM player_recent_cache WHERE fetched_at < $1", now - RECENT_TTL_SECONDS
                ),
                "matchrooms": await conn.execute(
                    "DELETE FROM matchroom_cache WHERE fetched_at < $1", now - MATCHROOM_TTL_SECONDS
                ),
                "matches": await conn.execute("DELETE FROM matches WHERE fetched_at < $1", now - MATCH_TTL_SECONDS),
                # Banned players stay: they're the useful rows, re-checked when recheck_at passes
                "clean_bans": await conn.execute(
                    "DELETE FROM player_bans WHERE is_banned = FALSE AND recheck_at < $1", now
                ),
            }
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _CLEANUP_LOCK)
    log.info("Cache cleanup: %s", results)
    return True


# ── players ───────────────────────────────────────────────────────────────────

_UPSERT_PLAYERS = """
    INSERT INTO players (player_id, nickname, steam_id_64, avatar, updated_at)
    SELECT t.*, EXTRACT(EPOCH FROM NOW())::int
    FROM unnest($1::text[], $2::text[], $3::text[], $4::text[]) AS t
    ON CONFLICT (player_id) DO UPDATE SET
        nickname    = EXCLUDED.nickname,
        steam_id_64 = CASE WHEN EXCLUDED.steam_id_64 != '' THEN EXCLUDED.steam_id_64
                           ELSE players.steam_id_64 END,
        avatar      = CASE WHEN EXCLUDED.avatar != '' THEN EXCLUDED.avatar
                           ELSE players.avatar END,
        updated_at  = EXCLUDED.updated_at
"""


async def upsert_players_bulk(players: list[tuple]) -> None:
    """Accepts (player_id, nickname[, steam_id_64[, avatar]]) tuples; one round trip."""
    latest: dict[str, tuple[str, str, str, str]] = {}
    for p in players:
        if not p or not p[0] or not p[1]:
            continue
        latest[p[0]] = (p[0], p[1], (p[2] if len(p) > 2 else "") or "", (p[3] if len(p) > 3 else "") or "")
    if not latest:
        return
    ids, nicks, steam_ids, avatars = zip(*latest.values())
    pool = await _get_pool()
    await pool.execute(_UPSERT_PLAYERS, list(ids), list(nicks), list(steam_ids), list(avatars))


async def upsert_player(player_id: str, nickname: str, steam_id_64: str = "", avatar: str = "") -> None:
    await upsert_players_bulk([(player_id, nickname, steam_id_64, avatar)])


async def get_players(player_ids: list[str]) -> dict[str, dict]:
    """{player_id: {nickname, steam_id_64, avatar}} for the ids we know."""
    if not player_ids:
        return {}
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT player_id, nickname, steam_id_64, avatar FROM players WHERE player_id = ANY($1)",
        list(set(player_ids)),
    )
    return {
        r["player_id"]: {"nickname": r["nickname"], "steam_id_64": r["steam_id_64"] or "", "avatar": r["avatar"] or ""}
        for r in rows
    }


async def get_nicknames(player_ids: list[str]) -> dict[str, str]:
    known = await get_players(player_ids)
    return {pid: known[pid]["nickname"] if pid in known else pid[:8] for pid in player_ids}


# ── matches ───────────────────────────────────────────────────────────────────


def _match_from_row(row) -> dict:
    return {
        "match_id": row["match_id"],
        "started_at": row["started_at"],
        "status": row["status"],
        "map": row["map"],
        "teams": _ensure_dict(row["teams_json"]),
        "score": _ensure_dict(row["score_json"]),
        "faceit_url": row["faceit_url"],
        "competition_type": row["competition_type"] or "",
        "competition_name": row["competition_name"] or "",
        "stats_fetched": row["stats_fetched"],
    }


async def get_cached_matches_bulk(match_ids: list[str]) -> dict[str, dict | None]:
    """{match_id: match | None} in one query."""
    if not match_ids:
        return {}
    pool = await _get_pool()
    rows = await pool.fetch("SELECT * FROM matches WHERE match_id = ANY($1)", list(match_ids))
    found = {row["match_id"]: _match_from_row(row) for row in rows}
    return {mid: found.get(mid) for mid in match_ids}


async def save_matches_bulk(matches: list[dict]) -> None:
    """Upsert match summaries (from history items or match details) in one round trip.

    A row whose details already came from /stats keeps its map and round score.
    """
    matches = list({m["match_id"]: m for m in matches}.values())  # one row per id, or the upsert fails
    if not matches:
        return
    cols = {k: [] for k in ("id", "start", "status", "map", "teams", "score", "url", "ctype", "cname")}
    for m in matches:
        cols["id"].append(m["match_id"])
        cols["start"].append(int(m.get("started_at") or 0))
        cols["status"].append(m.get("status") or "unknown")
        cols["map"].append(m.get("map") or "unknown")
        cols["teams"].append(json.dumps(m.get("teams") or {}))
        cols["score"].append(json.dumps(m.get("score") or {}))
        cols["url"].append(m.get("faceit_url") or "")
        cols["ctype"].append(m.get("competition_type") or "")
        cols["cname"].append(m.get("competition_name") or "")
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO matches (match_id, started_at, status, map, teams_json, score_json,
                             faceit_url, competition_type, competition_name)
        SELECT * FROM unnest($1::text[], $2::int[], $3::text[], $4::text[], $5::jsonb[], $6::jsonb[],
                             $7::text[], $8::text[], $9::text[])
        ON CONFLICT (match_id) DO UPDATE SET
            started_at       = EXCLUDED.started_at,
            status           = EXCLUDED.status,
            teams_json       = EXCLUDED.teams_json,
            map              = CASE WHEN matches.stats_fetched OR EXCLUDED.map = 'unknown'
                                    THEN matches.map ELSE EXCLUDED.map END,
            score_json       = CASE WHEN matches.stats_fetched THEN matches.score_json
                                    ELSE EXCLUDED.score_json END,
            faceit_url       = EXCLUDED.faceit_url,
            competition_type = EXCLUDED.competition_type,
            competition_name = EXCLUDED.competition_name,
            fetched_at       = EXTRACT(EPOCH FROM NOW())::int
        """,
        cols["id"],
        cols["start"],
        cols["status"],
        cols["map"],
        cols["teams"],
        cols["score"],
        cols["url"],
        cols["ctype"],
        cols["cname"],
    )


async def save_match_details(match_id: str, map_name: str | None, score: dict | None) -> None:
    """Record map and round score from /stats, or that /stats had nothing (both None)."""
    pool = await _get_pool()
    await pool.execute(
        """
        UPDATE matches SET
            map = COALESCE($2, map),
            score_json = COALESCE($3::jsonb, score_json),
            stats_fetched = TRUE
        WHERE match_id = $1
        """,
        match_id,
        map_name,
        json.dumps(score) if score is not None else None,
    )


# ── player_matches (each player's history index) ──────────────────────────────


async def link_player_matches_bulk(player_id: str, match_ids: list[str]) -> None:
    if not match_ids:
        return
    pool = await _get_pool()
    await pool.execute(
        "INSERT INTO player_matches (player_id, match_id) SELECT $1, unnest($2::text[]) ON CONFLICT DO NOTHING",
        player_id,
        list(match_ids),
    )


async def get_player_match_ids(player_id: str) -> set[str]:
    pool = await _get_pool()
    rows = await pool.fetch("SELECT match_id FROM player_matches WHERE player_id = $1", player_id)
    return {r["match_id"] for r in rows}


# ── player_scans ──────────────────────────────────────────────────────────────


async def get_player_scan_info(player_id: str) -> dict | None:
    """Return {scanned_at, total_matches} or None if never fully scanned."""
    pool = await _get_pool()
    row = await pool.fetchrow("SELECT scanned_at, total_matches FROM player_scans WHERE player_id = $1", player_id)
    if not row:
        return None
    return {"scanned_at": row["scanned_at"], "total_matches": row["total_matches"]}


async def mark_player_scanned(player_id: str, total_matches: int) -> None:
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO player_scans (player_id, scanned_at, total_matches)
        VALUES ($1, EXTRACT(EPOCH FROM NOW())::int, $2)
        ON CONFLICT (player_id) DO UPDATE SET
            scanned_at    = EXCLUDED.scanned_at,
            total_matches = EXCLUDED.total_matches
        """,
        player_id,
        total_matches,
    )


# ── player stats caches ───────────────────────────────────────────────────────


async def _get_json(table: str, column: str, player_id: str, ttl: int):
    pool = await _get_pool()
    row = await pool.fetchrow(f"SELECT {column}, fetched_at FROM {table} WHERE player_id = $1", player_id)
    if not row or time.time() - (row["fetched_at"] or 0) > ttl:
        return None
    val = row[column]
    return json.loads(val) if isinstance(val, str) else val


async def _put_json(table: str, column: str, player_id: str, value) -> None:
    pool = await _get_pool()
    await pool.execute(
        f"""
        INSERT INTO {table} (player_id, {column}, fetched_at)
        VALUES ($1, $2::jsonb, EXTRACT(EPOCH FROM NOW())::int)
        ON CONFLICT (player_id) DO UPDATE SET {column} = EXCLUDED.{column}, fetched_at = EXCLUDED.fetched_at
        """,
        player_id,
        json.dumps(value),
    )


async def get_cached_player_stats(player_id: str) -> dict | None:
    return await _get_json("player_stats_cache", "stats_json", player_id, STATS_TTL_SECONDS)


async def save_player_stats(player_id: str, stats: dict) -> None:
    await _put_json("player_stats_cache", "stats_json", player_id, stats)


async def get_cached_player_recent(player_id: str) -> list[dict] | None:
    """Recent match rows from /games/cs2/stats, or None when missing or older than RECENT_TTL."""
    return await _get_json("player_recent_cache", "rows_json", player_id, RECENT_TTL_SECONDS)


async def save_player_recent(player_id: str, rows: list[dict]) -> None:
    await _put_json("player_recent_cache", "rows_json", player_id, rows)


# ── matchroom cache ──────────────────────────────────────────────────────────


async def get_cached_matchroom(match_id: str) -> dict | None:
    pool = await _get_pool()
    row = await pool.fetchrow("SELECT data_json, fetched_at FROM matchroom_cache WHERE match_id = $1", match_id)
    if not row or time.time() - (row["fetched_at"] or 0) > MATCHROOM_TTL_SECONDS:
        return None
    data = _ensure_dict(row["data_json"])
    # Only finished rooms are served from cache; a live one changes during veto
    if str(data.get("status", "")).upper() not in FINAL_MATCH_STATUSES:
        return None
    return data


async def save_matchroom(match_id: str, data: dict) -> None:
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO matchroom_cache (match_id, data_json, fetched_at)
        VALUES ($1, $2::jsonb, EXTRACT(EPOCH FROM NOW())::int)
        ON CONFLICT (match_id) DO UPDATE SET data_json = EXCLUDED.data_json, fetched_at = EXCLUDED.fetched_at
        """,
        match_id,
        json.dumps(data),
    )


# ── player ban cache (adaptive TTL) ──────────────────────────────────────────


def parse_ts(value: str | None) -> int | None:
    """FACEIT ISO timestamp ("2024-05-01T10:00:00Z") → epoch seconds."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _compute_ban_recheck_at(is_banned: bool, bans: list[dict], now: int | None = None) -> int:
    """When to re-check this player's bans.

    - Clean player, or every ban already ended → now + 7 days
    - Ban with an end date ahead              → end + 6 h
    - Ban without (or with an unreadable) end, or ending 2+ years out → now + 90 days
    """
    now = int(time.time()) if now is None else now
    if not is_banned:
        return now + BAN_CLEAN_TTL_SECONDS

    latest_end = 0
    for b in bans:
        end_ts = parse_ts(b.get("ends_at"))
        if end_ts is None:
            return now + BAN_PERMANENT_TTL_SECONDS
        latest_end = max(latest_end, end_ts)

    if latest_end <= now:
        return now + BAN_CLEAN_TTL_SECONDS
    if latest_end - now > 2 * 365 * 24 * 3600:
        return now + BAN_PERMANENT_TTL_SECONDS
    return latest_end + BAN_EXPIRY_BUFFER_SECONDS


async def get_cached_ban_status(player_ids: list[str]) -> dict[str, dict | None]:
    """Cached ban info per player id; None when missing or past recheck_at."""
    if not player_ids:
        return {}
    now = int(time.time())
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT player_id, is_banned, bans_json, checked_at, recheck_at FROM player_bans WHERE player_id = ANY($1)",
        list(player_ids),
    )
    result: dict[str, dict | None] = {pid: None for pid in player_ids}
    for r in rows:
        if now >= (r["recheck_at"] or 0):
            continue
        bans_val = r["bans_json"]
        if isinstance(bans_val, str):
            bans_val = json.loads(bans_val)
        result[r["player_id"]] = {"is_banned": r["is_banned"], "bans": bans_val or [], "checked_at": r["checked_at"]}
    return result


async def save_ban_statuses_bulk(results: list[tuple[str, bool, list[dict]]]) -> None:
    """Bulk upsert ban results — single roundtrip."""
    if not results:
        return
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO player_bans (player_id, is_banned, bans_json, checked_at, recheck_at)
        SELECT unnest($1::text[]), unnest($2::boolean[]), unnest($3::jsonb[]),
               EXTRACT(EPOCH FROM NOW())::int, unnest($4::int[])
        ON CONFLICT (player_id) DO UPDATE SET
            is_banned  = EXCLUDED.is_banned,
            bans_json  = EXCLUDED.bans_json,
            checked_at = EXCLUDED.checked_at,
            recheck_at = EXCLUDED.recheck_at
        """,
        [r[0] for r in results],
        [r[1] for r in results],
        [json.dumps(r[2]) for r in results],
        [_compute_ban_recheck_at(r[1], r[2]) for r in results],
    )


# ── helpers ───────────────────────────────────────────────────────────────────


def _ensure_dict(val) -> dict:
    """asyncpg returns JSONB as text unless a codec is set; accept both."""
    if isinstance(val, str):
        return json.loads(val)
    return val if val is not None else {}
