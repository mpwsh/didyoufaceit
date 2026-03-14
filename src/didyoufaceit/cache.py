"""Async PostgreSQL cache for FACEIT match data and player scan state.

Tuned for a single-app consumer with bursty parallel inserts.
Pool is kept small; bulk ops use unnest/VALUES batching instead of executemany.
Stale cache rows are cleaned up periodically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import asyncpg

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://didyou:didyou@localhost:5432/didyou")

# ── TTL configuration ─────────────────────────────────────────────────────────

STATS_TTL_SECONDS = 3 * 24 * 3600  # 3 days  — player lifetime stats
MATCHROOM_TTL_SECONDS = 7 * 24 * 3600  # 7 days  — matchroom snapshots
MATCH_TTL_SECONDS = 90 * 24 * 3600  # 90 days — individual match data
PLAYER_TTL_SECONDS = 30 * 24 * 3600  # 30 days — player nickname cache
SCAN_TTL_SECONDS = 6 * 3600  # 6 hours — before re-fetching recent history

# Ban cache — per-player adaptive TTL:
#   Clean player     → re-check after 7 days
#   Active ban       → re-check after ban expires (ban_end + small buffer)
#   Permanent ban    → re-check every 90 days
BAN_CLEAN_TTL_SECONDS = 7 * 24 * 3600
BAN_PERMANENT_TTL_SECONDS = 90 * 24 * 3600
BAN_EXPIRY_BUFFER_SECONDS = 6 * 3600  # re-check 6h after a ban is supposed to lift

CLEANUP_INTERVAL_SECONDS = 6 * 3600  # run cleanup every 6 hours

# ── pool configuration ────────────────────────────────────────────────────────
# Single consumer app — keep the pool small so Postgres has room for
# autovacuum workers, replication slots, etc.  max_size=5 is plenty for
# our concurrency (FACEIT API semaphore is 5 and that's the bottleneck).

POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5
POOL_COMMAND_TIMEOUT = 15  # kill queries that hang
POOL_MAX_INACTIVE_CONNECTION_LIFETIME = 300  # drop idle conns after 5 min

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
                DATABASE_URL,
                min_size=POOL_MIN_SIZE,
                max_size=POOL_MAX_SIZE,
                command_timeout=POOL_COMMAND_TIMEOUT,
                max_inactive_connection_lifetime=POOL_MAX_INACTIVE_CONNECTION_LIFETIME,
            )
    return _pool


# ── lifecycle ─────────────────────────────────────────────────────────────────


async def init_db() -> None:
    """Create tables if they don't exist and start background cleanup."""
    global _cleanup_task
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
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

            CREATE INDEX IF NOT EXISTS idx_player_matches_player ON player_matches(player_id);
            CREATE INDEX IF NOT EXISTS idx_player_matches_match  ON player_matches(match_id);
            CREATE INDEX IF NOT EXISTS idx_matches_fetched       ON matches(fetched_at);
            CREATE INDEX IF NOT EXISTS idx_players_updated       ON players(updated_at);
            CREATE INDEX IF NOT EXISTS idx_stats_fetched         ON player_stats_cache(fetched_at);
            CREATE INDEX IF NOT EXISTS idx_matchroom_fetched     ON matchroom_cache(fetched_at);
            CREATE INDEX IF NOT EXISTS idx_bans_recheck          ON player_bans(recheck_at);
        """)

        # ── Migrations for existing databases ────────────────────────────────
        # Add steam_id_64 to players if it doesn't exist yet
        col_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'players' AND column_name = 'steam_id_64'
            )
        """)
        if not col_exists:
            await conn.execute("ALTER TABLE players ADD COLUMN steam_id_64 TEXT DEFAULT ''")
            log.info("Migration: added steam_id_64 column to players table")

        # Add avatar to players if it doesn't exist yet
        avatar_exists = await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'players' AND column_name = 'avatar'
            )
        """)
        if not avatar_exists:
            await conn.execute("ALTER TABLE players ADD COLUMN avatar TEXT DEFAULT ''")
            log.info("Migration: added avatar column to players table")
    log.info("Database schema initialized")

    # Start background TTL cleanup
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_loop())
        log.info("Started background cache cleanup task")


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
    """Periodically delete expired cache rows."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            await cleanup_expired()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Cache cleanup error (will retry next cycle)")


async def cleanup_expired() -> None:
    """Delete rows older than their TTL.  Safe to call anytime."""
    now = int(time.time())
    pool = await _get_pool()
    async with pool.acquire() as conn:
        r1 = await conn.execute(
            "DELETE FROM player_stats_cache WHERE fetched_at < $1",
            now - STATS_TTL_SECONDS,
        )
        r2 = await conn.execute(
            "DELETE FROM matchroom_cache WHERE fetched_at < $1",
            now - MATCHROOM_TTL_SECONDS,
        )
        r3 = await conn.execute(
            "DELETE FROM matches WHERE fetched_at < $1",
            now - MATCH_TTL_SECONDS,
        )
        r4 = await conn.execute(
            "DELETE FROM players WHERE updated_at < $1",
            now - PLAYER_TTL_SECONDS,
        )
        # Clean up orphaned player_matches links (match was evicted)
        r5 = await conn.execute("""
            DELETE FROM player_matches pm
            WHERE NOT EXISTS (SELECT 1 FROM matches m WHERE m.match_id = pm.match_id)
        """)
        # Clean up stale ban cache for CLEAN players only.
        # Banned players are kept (they're still useful info) but will be
        # re-fetched when recheck_at passes on the next scan.
        r6 = await conn.execute(
            "DELETE FROM player_bans WHERE is_banned = FALSE AND recheck_at < $1",
            now,
        )
    log.info(
        "Cache cleanup: stats=%s matchrooms=%s matches=%s players=%s orphan_links=%s bans=%s",
        r1,
        r2,
        r3,
        r4,
        r5,
        r6,
    )


# ── players ───────────────────────────────────────────────────────────────────


async def upsert_player(player_id: str, nickname: str, steam_id_64: str = "", avatar: str = "") -> None:
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO players (player_id, nickname, steam_id_64, avatar, updated_at)
        VALUES ($1, $2, $3, $4, EXTRACT(EPOCH FROM NOW())::int)
        ON CONFLICT (player_id) DO UPDATE SET
            nickname    = EXCLUDED.nickname,
            steam_id_64 = CASE WHEN EXCLUDED.steam_id_64 != '' THEN EXCLUDED.steam_id_64
                               ELSE players.steam_id_64 END,
            avatar      = CASE WHEN EXCLUDED.avatar != '' THEN EXCLUDED.avatar
                               ELSE players.avatar END,
            updated_at  = EXCLUDED.updated_at
        """,
        player_id,
        nickname,
        steam_id_64 or "",
        avatar or "",
    )


async def upsert_players_bulk(players: list[tuple]) -> None:
    """Bulk upsert using unnest — single roundtrip instead of N.

    Accepts tuples of (player_id, nickname), (player_id, nickname, steam_id_64),
    or (player_id, nickname, steam_id_64, avatar).
    """
    if not players:
        return
    # Normalize to 4-tuples
    normalized = [(p[0], p[1], p[2] if len(p) > 2 else "", p[3] if len(p) > 3 else "") for p in players]
    ids, nicks, steam_ids, avatars = zip(*normalized)
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO players (player_id, nickname, steam_id_64, avatar, updated_at)
        SELECT unnest($1::text[]), unnest($2::text[]), unnest($3::text[]),
               unnest($4::text[]), EXTRACT(EPOCH FROM NOW())::int
        ON CONFLICT (player_id) DO UPDATE SET
            nickname    = EXCLUDED.nickname,
            steam_id_64 = CASE WHEN EXCLUDED.steam_id_64 != '' THEN EXCLUDED.steam_id_64
                               ELSE players.steam_id_64 END,
            avatar      = CASE WHEN EXCLUDED.avatar != '' THEN EXCLUDED.avatar
                               ELSE players.avatar END,
            updated_at  = EXCLUDED.updated_at
        """,
        list(ids),
        list(nicks),
        list(steam_ids),
        list(avatars),
    )


async def get_nicknames(player_ids: list[str]) -> dict[str, str]:
    if not player_ids:
        return {}
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT player_id, nickname FROM players WHERE player_id = ANY($1)",
        player_ids,
    )
    result = {r["player_id"]: r["nickname"] for r in rows}
    for pid in player_ids:
        if pid not in result:
            result[pid] = pid[:8]
    return result


async def get_steam_ids(player_ids: list[str]) -> dict[str, str]:
    """Bulk fetch steam_id_64 for a list of player IDs. Returns {player_id: steam_id_64}."""
    if not player_ids:
        return {}
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT player_id, steam_id_64 FROM players WHERE player_id = ANY($1) AND steam_id_64 != ''",
        player_ids,
    )
    return {r["player_id"]: r["steam_id_64"] for r in rows}


async def get_avatars(player_ids: list[str]) -> dict[str, str]:
    """Bulk fetch avatar URLs for a list of player IDs. Returns {player_id: avatar_url}."""
    if not player_ids:
        return {}
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT player_id, avatar FROM players WHERE player_id = ANY($1) AND avatar != ''",
        player_ids,
    )
    return {r["player_id"]: r["avatar"] for r in rows}


# ── matches ───────────────────────────────────────────────────────────────────


async def get_cached_matches_bulk(match_ids: list[str]) -> dict[str, dict | None]:
    """Fetch multiple matches in a single query.  Returns {match_id: data | None}."""
    if not match_ids:
        return {}
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT * FROM matches WHERE match_id = ANY($1)",
        match_ids,
    )
    found = {}
    for row in rows:
        found[row["match_id"]] = {
            "match_id": row["match_id"],
            "started_at": row["started_at"],
            "status": row["status"],
            "map": row["map"],
            "teams": _ensure_dict(row["teams_json"]),
            "score": _ensure_dict(row["score_json"]),
            "faceit_url": row["faceit_url"],
            "competition_type": row["competition_type"] or "",
            "competition_name": row["competition_name"] or "",
        }
    # Fill in misses as None
    return {mid: found.get(mid) for mid in match_ids}


async def get_cached_match(match_id: str) -> dict | None:
    results = await get_cached_matches_bulk([match_id])
    return results.get(match_id)


async def save_match(
    match_id: str,
    started_at: int,
    status: str,
    map_name: str,
    teams: dict,
    score: dict,
    faceit_url: str,
    competition_type: str = "",
    competition_name: str = "",
) -> None:
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO matches (match_id, started_at, status, map, teams_json, score_json,
                             faceit_url, competition_type, competition_name)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9)
        ON CONFLICT (match_id) DO UPDATE SET
            started_at       = EXCLUDED.started_at,
            status           = EXCLUDED.status,
            map              = EXCLUDED.map,
            teams_json       = EXCLUDED.teams_json,
            score_json       = EXCLUDED.score_json,
            faceit_url       = EXCLUDED.faceit_url,
            competition_type = EXCLUDED.competition_type,
            competition_name = EXCLUDED.competition_name
        """,
        match_id,
        started_at,
        status,
        map_name,
        json.dumps(teams),
        json.dumps(score),
        faceit_url,
        competition_type,
        competition_name,
    )


async def update_match_score(match_id: str, score: dict) -> None:
    pool = await _get_pool()
    await pool.execute(
        "UPDATE matches SET score_json = $1::jsonb WHERE match_id = $2",
        json.dumps(score),
        match_id,
    )


# ── player_matches ────────────────────────────────────────────────────────────


async def link_player_match(player_id: str, match_id: str) -> None:
    pool = await _get_pool()
    await pool.execute(
        "INSERT INTO player_matches (player_id, match_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        player_id,
        match_id,
    )


async def link_player_matches_bulk(player_id: str, match_ids: list[str]) -> None:
    """Bulk link using unnest — single roundtrip for thousands of rows."""
    if not match_ids:
        return
    pool = await _get_pool()
    # Build parallel arrays: [player_id, player_id, ...] and [mid1, mid2, ...]
    await pool.execute(
        """
        INSERT INTO player_matches (player_id, match_id)
        SELECT $1, unnest($2::text[])
        ON CONFLICT DO NOTHING
        """,
        player_id,
        match_ids,
    )


async def get_player_match_ids(player_id: str) -> set[str]:
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT match_id FROM player_matches WHERE player_id = $1",
        player_id,
    )
    return {r["match_id"] for r in rows}


# ── player_scans ──────────────────────────────────────────────────────────────


async def is_player_fully_scanned(player_id: str) -> bool:
    pool = await _get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM player_scans WHERE player_id = $1",
        player_id,
    )
    return row is not None


async def get_player_scan_info(player_id: str) -> dict | None:
    """Return {scanned_at, total_matches} or None if never scanned."""
    pool = await _get_pool()
    row = await pool.fetchrow(
        "SELECT scanned_at, total_matches FROM player_scans WHERE player_id = $1",
        player_id,
    )
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


# ── player stats cache ───────────────────────────────────────────────────────


async def get_cached_player_stats(player_id: str) -> dict | None:
    pool = await _get_pool()
    row = await pool.fetchrow(
        "SELECT stats_json, fetched_at FROM player_stats_cache WHERE player_id = $1",
        player_id,
    )
    if not row:
        return None
    if time.time() - (row["fetched_at"] or 0) > STATS_TTL_SECONDS:
        return None
    return _ensure_dict(row["stats_json"])


async def save_player_stats(player_id: str, stats: dict) -> None:
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO player_stats_cache (player_id, stats_json, fetched_at)
        VALUES ($1, $2::jsonb, EXTRACT(EPOCH FROM NOW())::int)
        ON CONFLICT (player_id) DO UPDATE SET
            stats_json = EXCLUDED.stats_json,
            fetched_at = EXCLUDED.fetched_at
        """,
        player_id,
        json.dumps(stats),
    )


# ── matchroom cache ──────────────────────────────────────────────────────────


async def get_cached_matchroom(match_id: str) -> dict | None:
    pool = await _get_pool()
    row = await pool.fetchrow(
        "SELECT data_json, fetched_at FROM matchroom_cache WHERE match_id = $1",
        match_id,
    )
    if not row:
        return None
    if time.time() - (row["fetched_at"] or 0) > MATCHROOM_TTL_SECONDS:
        return None
    return _ensure_dict(row["data_json"])


async def save_matchroom(match_id: str, data: dict) -> None:
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO matchroom_cache (match_id, data_json, fetched_at)
        VALUES ($1, $2::jsonb, EXTRACT(EPOCH FROM NOW())::int)
        ON CONFLICT (match_id) DO UPDATE SET
            data_json  = EXCLUDED.data_json,
            fetched_at = EXCLUDED.fetched_at
        """,
        match_id,
        json.dumps(data),
    )


# ── player ban cache (adaptive TTL) ──────────────────────────────────────────


def _compute_ban_recheck_at(is_banned: bool, bans: list[dict]) -> int:
    """Compute when to re-check this player's ban status.

    - Clean player → now + 7 days
    - Has ban with end date → ban_end + 6h buffer
    - Permanent ban (no end date / far future) → now + 90 days
    """
    now = int(time.time())

    if not is_banned:
        return now + BAN_CLEAN_TTL_SECONDS

    # Find the latest ban end date
    latest_end = 0
    for b in bans:
        end_str = b.get("ends_at", "")
        if not end_str:
            # No end date → permanent ban
            return now + BAN_PERMANENT_TTL_SECONDS
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            end_ts = int(dt.timestamp())
            latest_end = max(latest_end, end_ts)
        except (ValueError, TypeError):
            # Can't parse → treat as permanent
            return now + BAN_PERMANENT_TTL_SECONDS

    if latest_end == 0:
        # Has bans but no parseable end dates
        return now + BAN_PERMANENT_TTL_SECONDS

    # If ban end is very far out (>2 years), treat as permanent
    if latest_end - now > 2 * 365 * 24 * 3600:
        return now + BAN_PERMANENT_TTL_SECONDS

    # Re-check shortly after ban is supposed to lift
    return latest_end + BAN_EXPIRY_BUFFER_SECONDS


async def get_cached_ban_status(player_ids: list[str]) -> dict[str, dict | None]:
    """Return cached ban info for player IDs.

    Returns None for players whose cache has expired (recheck_at in the past).
    """
    if not player_ids:
        return {}
    now = int(time.time())
    pool = await _get_pool()
    rows = await pool.fetch(
        """SELECT player_id, is_banned, bans_json, checked_at, recheck_at
           FROM player_bans
           WHERE player_id = ANY($1)""",
        player_ids,
    )
    result: dict[str, dict | None] = {pid: None for pid in player_ids}
    for r in rows:
        # Cache is stale if we've passed the recheck time
        if now >= (r["recheck_at"] or 0):
            result[r["player_id"]] = None
            continue
        bans_val = r["bans_json"]
        if isinstance(bans_val, str):
            bans_val = json.loads(bans_val)
        elif bans_val is None:
            bans_val = []
        result[r["player_id"]] = {
            "is_banned": r["is_banned"],
            "bans": bans_val,
            "checked_at": r["checked_at"],
        }
    return result


async def save_ban_status(player_id: str, is_banned: bool, bans: list[dict]) -> None:
    recheck_at = _compute_ban_recheck_at(is_banned, bans)
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO player_bans (player_id, is_banned, bans_json, checked_at, recheck_at)
        VALUES ($1, $2, $3::jsonb, EXTRACT(EPOCH FROM NOW())::int, $4)
        ON CONFLICT (player_id) DO UPDATE SET
            is_banned  = EXCLUDED.is_banned,
            bans_json  = EXCLUDED.bans_json,
            checked_at = EXCLUDED.checked_at,
            recheck_at = EXCLUDED.recheck_at
        """,
        player_id,
        is_banned,
        json.dumps(bans),
        recheck_at,
    )


async def save_ban_statuses_bulk(results: list[tuple[str, bool, list[dict]]]) -> None:
    """Bulk upsert ban results — single roundtrip."""
    if not results:
        return
    ids = [r[0] for r in results]
    banned_flags = [r[1] for r in results]
    bans_jsons = [json.dumps(r[2]) for r in results]
    recheck_ats = [_compute_ban_recheck_at(r[1], r[2]) for r in results]
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
        ids,
        banned_flags,
        bans_jsons,
        recheck_ats,
    )


# ── stats ─────────────────────────────────────────────────────────────────────


async def get_db_stats() -> dict:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        matches = await conn.fetchval("SELECT COUNT(*) FROM matches")
        players = await conn.fetchval("SELECT COUNT(*) FROM players")
        links = await conn.fetchval("SELECT COUNT(*) FROM player_matches")
    return {
        "cached_matches": matches,
        "known_players": players,
        "player_match_links": links,
    }


# ── helpers ───────────────────────────────────────────────────────────────────


def _ensure_dict(val) -> dict:
    """asyncpg returns JSONB as already-parsed dicts, but handle strings too."""
    if isinstance(val, str):
        return json.loads(val)
    return val if val is not None else {}
