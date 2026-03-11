"""Async PostgreSQL cache for FACEIT match data and player scan state."""

from __future__ import annotations

import json
import logging
import os

import asyncpg

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://didyou:didyou@localhost:5432/didyou")

STATS_TTL_SECONDS = 3 * 24 * 3600  # 3 days

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _pool


async def init_db() -> None:
    """Create tables if they don't exist."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                player_id   TEXT PRIMARY KEY,
                nickname    TEXT NOT NULL,
                updated_at  INT DEFAULT (EXTRACT(EPOCH FROM NOW())::int)
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

            -- Indexes for common lookups
            CREATE INDEX IF NOT EXISTS idx_player_matches_player ON player_matches(player_id);
            CREATE INDEX IF NOT EXISTS idx_player_matches_match ON player_matches(match_id);
        """)
    log.info("Database schema initialized")


async def close_db() -> None:
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ── players ───────────────────────────────────────────────────────────────────

async def upsert_player(player_id: str, nickname: str) -> None:
    pool = await _get_pool()
    await pool.execute("""
        INSERT INTO players (player_id, nickname, updated_at)
        VALUES ($1, $2, EXTRACT(EPOCH FROM NOW())::int)
        ON CONFLICT (player_id) DO UPDATE SET
            nickname = EXCLUDED.nickname,
            updated_at = EXTRACT(EPOCH FROM NOW())::int
    """, player_id, nickname)


async def upsert_players_bulk(players: list[tuple[str, str]]) -> None:
    if not players:
        return
    pool = await _get_pool()
    await pool.executemany("""
        INSERT INTO players (player_id, nickname, updated_at)
        VALUES ($1, $2, EXTRACT(EPOCH FROM NOW())::int)
        ON CONFLICT (player_id) DO UPDATE SET
            nickname = EXCLUDED.nickname,
            updated_at = EXTRACT(EPOCH FROM NOW())::int
    """, players)


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


# ── matches ───────────────────────────────────────────────────────────────────

async def get_cached_match(match_id: str) -> dict | None:
    pool = await _get_pool()
    row = await pool.fetchrow("SELECT * FROM matches WHERE match_id = $1", match_id)
    if not row:
        return None
    return {
        "match_id": row["match_id"], "started_at": row["started_at"],
        "status": row["status"], "map": row["map"],
        "teams": _ensure_dict(row["teams_json"]),
        "score": _ensure_dict(row["score_json"]),
        "faceit_url": row["faceit_url"],
        "competition_type": row["competition_type"] or "",
        "competition_name": row["competition_name"] or "",
    }


async def save_match(match_id: str, started_at: int, status: str, map_name: str,
                     teams: dict, score: dict, faceit_url: str,
                     competition_type: str = "", competition_name: str = "") -> None:
    pool = await _get_pool()
    await pool.execute("""
        INSERT INTO matches (match_id, started_at, status, map, teams_json, score_json,
                             faceit_url, competition_type, competition_name)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9)
        ON CONFLICT (match_id) DO UPDATE SET
            started_at = EXCLUDED.started_at,
            status = EXCLUDED.status,
            map = EXCLUDED.map,
            teams_json = EXCLUDED.teams_json,
            score_json = EXCLUDED.score_json,
            faceit_url = EXCLUDED.faceit_url,
            competition_type = EXCLUDED.competition_type,
            competition_name = EXCLUDED.competition_name
    """, match_id, started_at, status, map_name,
         json.dumps(teams), json.dumps(score), faceit_url,
         competition_type, competition_name)


async def update_match_score(match_id: str, score: dict) -> None:
    pool = await _get_pool()
    await pool.execute(
        "UPDATE matches SET score_json = $1::jsonb WHERE match_id = $2",
        json.dumps(score), match_id)


# ── player_matches ────────────────────────────────────────────────────────────

async def link_player_match(player_id: str, match_id: str) -> None:
    pool = await _get_pool()
    await pool.execute(
        "INSERT INTO player_matches (player_id, match_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        player_id, match_id)


async def link_player_matches_bulk(player_id: str, match_ids: list[str]) -> None:
    if not match_ids:
        return
    pool = await _get_pool()
    await pool.executemany(
        "INSERT INTO player_matches (player_id, match_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        [(player_id, mid) for mid in match_ids])


async def get_player_match_ids(player_id: str) -> set[str]:
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT match_id FROM player_matches WHERE player_id = $1", player_id)
    return {r["match_id"] for r in rows}


# ── player_scans ──────────────────────────────────────────────────────────────

async def is_player_fully_scanned(player_id: str) -> bool:
    pool = await _get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM player_scans WHERE player_id = $1", player_id)
    return row is not None


async def mark_player_scanned(player_id: str, total_matches: int) -> None:
    pool = await _get_pool()
    await pool.execute("""
        INSERT INTO player_scans (player_id, scanned_at, total_matches)
        VALUES ($1, EXTRACT(EPOCH FROM NOW())::int, $2)
        ON CONFLICT (player_id) DO UPDATE SET
            scanned_at = EXTRACT(EPOCH FROM NOW())::int,
            total_matches = EXCLUDED.total_matches
    """, player_id, total_matches)


# ── player stats cache (3-day TTL) ───────────────────────────────────────────

async def get_cached_player_stats(player_id: str) -> dict | None:
    pool = await _get_pool()
    row = await pool.fetchrow(
        "SELECT stats_json, fetched_at FROM player_stats_cache WHERE player_id = $1",
        player_id)
    if not row:
        return None
    import time
    if time.time() - (row["fetched_at"] or 0) > STATS_TTL_SECONDS:
        return None
    return _ensure_dict(row["stats_json"])


async def save_player_stats(player_id: str, stats: dict) -> None:
    pool = await _get_pool()
    await pool.execute("""
        INSERT INTO player_stats_cache (player_id, stats_json, fetched_at)
        VALUES ($1, $2::jsonb, EXTRACT(EPOCH FROM NOW())::int)
        ON CONFLICT (player_id) DO UPDATE SET
            stats_json = EXCLUDED.stats_json,
            fetched_at = EXTRACT(EPOCH FROM NOW())::int
    """, player_id, json.dumps(stats))


# ── matchroom cache (permanent) ──────────────────────────────────────────────

async def get_cached_matchroom(match_id: str) -> dict | None:
    pool = await _get_pool()
    row = await pool.fetchrow(
        "SELECT data_json FROM matchroom_cache WHERE match_id = $1", match_id)
    if not row:
        return None
    return _ensure_dict(row["data_json"])


async def save_matchroom(match_id: str, data: dict) -> None:
    pool = await _get_pool()
    await pool.execute("""
        INSERT INTO matchroom_cache (match_id, data_json, fetched_at)
        VALUES ($1, $2::jsonb, EXTRACT(EPOCH FROM NOW())::int)
        ON CONFLICT (match_id) DO UPDATE SET
            data_json = EXCLUDED.data_json,
            fetched_at = EXTRACT(EPOCH FROM NOW())::int
    """, match_id, json.dumps(data))


# ── stats ─────────────────────────────────────────────────────────────────────

async def get_db_stats() -> dict:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        matches = await conn.fetchval("SELECT COUNT(*) FROM matches")
        players = await conn.fetchval("SELECT COUNT(*) FROM players")
        links   = await conn.fetchval("SELECT COUNT(*) FROM player_matches")
    return {"cached_matches": matches, "known_players": players, "player_match_links": links}


# ── helpers ───────────────────────────────────────────────────────────────────

def _ensure_dict(val) -> dict:
    """asyncpg returns JSONB as already-parsed dicts, but handle strings too."""
    if isinstance(val, str):
        return json.loads(val)
    return val if val is not None else {}
