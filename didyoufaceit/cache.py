"""Async SQLite cache for FACEIT match data and player scan state."""

from __future__ import annotations

import json
import time
import aiosqlite
from pathlib import Path

DB_PATH = Path.home() / ".faceit_finder" / "cache.db"

STATS_TTL_SECONDS = 3 * 24 * 3600  # 3 days


async def _connect() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn


async def init_db() -> None:
    async with await _connect() as conn:
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                player_id   TEXT PRIMARY KEY,
                nickname    TEXT NOT NULL,
                updated_at  INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS matches (
                match_id        TEXT PRIMARY KEY,
                started_at      INTEGER,
                status          TEXT,
                map             TEXT,
                teams_json      TEXT,
                score_json      TEXT,
                faceit_url      TEXT,
                competition_type TEXT,
                competition_name TEXT,
                fetched_at      INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS player_matches (
                player_id   TEXT NOT NULL,
                match_id    TEXT NOT NULL,
                PRIMARY KEY (player_id, match_id)
            );
            CREATE TABLE IF NOT EXISTS player_scans (
                player_id       TEXT PRIMARY KEY,
                scanned_at      INTEGER DEFAULT (strftime('%s','now')),
                total_matches   INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS player_stats_cache (
                player_id    TEXT PRIMARY KEY,
                stats_json   TEXT NOT NULL,
                fetched_at   INTEGER DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS matchroom_cache (
                match_id      TEXT PRIMARY KEY,
                data_json     TEXT NOT NULL,
                fetched_at    INTEGER DEFAULT (strftime('%s','now'))
            );
        """)
        cursor = await conn.execute("PRAGMA table_info(matches)")
        match_cols = {r[1] for r in await cursor.fetchall()}
        for col, typedef in [("competition_type", "TEXT"), ("competition_name", "TEXT")]:
            if col not in match_cols:
                await conn.execute(f"ALTER TABLE matches ADD COLUMN {col} {typedef}")
        await conn.commit()


# ── players ───────────────────────────────────────────────────────────────────

async def upsert_player(player_id: str, nickname: str) -> None:
    async with await _connect() as conn:
        await conn.execute("""
            INSERT INTO players (player_id, nickname, updated_at)
            VALUES (?, ?, strftime('%s','now'))
            ON CONFLICT(player_id) DO UPDATE SET
                nickname = excluded.nickname, updated_at = excluded.updated_at
        """, (player_id, nickname))
        await conn.commit()


async def upsert_players_bulk(players: list[tuple[str, str]]) -> None:
    async with await _connect() as conn:
        await conn.executemany("""
            INSERT INTO players (player_id, nickname, updated_at)
            VALUES (?, ?, strftime('%s','now'))
            ON CONFLICT(player_id) DO UPDATE SET
                nickname = excluded.nickname, updated_at = excluded.updated_at
        """, players)
        await conn.commit()


async def get_nicknames(player_ids: list[str]) -> dict[str, str]:
    if not player_ids:
        return {}
    placeholders = ",".join("?" * len(player_ids))
    async with await _connect() as conn:
        cursor = await conn.execute(
            f"SELECT player_id, nickname FROM players WHERE player_id IN ({placeholders})",
            player_ids,
        )
        rows = await cursor.fetchall()
    result = {r["player_id"]: r["nickname"] for r in rows}
    for pid in player_ids:
        if pid not in result:
            result[pid] = pid[:8]
    return result


# ── matches ───────────────────────────────────────────────────────────────────

async def get_cached_match(match_id: str) -> dict | None:
    async with await _connect() as conn:
        cursor = await conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    return {
        "match_id": row["match_id"], "started_at": row["started_at"],
        "status": row["status"], "map": row["map"],
        "teams": json.loads(row["teams_json"]), "score": json.loads(row["score_json"]),
        "faceit_url": row["faceit_url"],
        "competition_type": row["competition_type"] or "",
        "competition_name": row["competition_name"] or "",
    }


async def save_match(match_id: str, started_at: int, status: str, map_name: str,
                     teams: dict, score: dict, faceit_url: str,
                     competition_type: str = "", competition_name: str = "") -> None:
    async with await _connect() as conn:
        await conn.execute("""
            INSERT OR REPLACE INTO matches
              (match_id, started_at, status, map, teams_json, score_json,
               faceit_url, competition_type, competition_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (match_id, started_at, status, map_name,
              json.dumps(teams), json.dumps(score), faceit_url,
              competition_type, competition_name))
        await conn.commit()


async def update_match_score(match_id: str, score: dict) -> None:
    async with await _connect() as conn:
        await conn.execute("UPDATE matches SET score_json = ? WHERE match_id = ?",
                           (json.dumps(score), match_id))
        await conn.commit()


# ── player_matches ────────────────────────────────────────────────────────────

async def link_player_match(player_id: str, match_id: str) -> None:
    async with await _connect() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO player_matches (player_id, match_id) VALUES (?, ?)",
            (player_id, match_id))
        await conn.commit()


async def link_player_matches_bulk(player_id: str, match_ids: list[str]) -> None:
    async with await _connect() as conn:
        await conn.executemany(
            "INSERT OR IGNORE INTO player_matches (player_id, match_id) VALUES (?, ?)",
            [(player_id, mid) for mid in match_ids])
        await conn.commit()


async def get_player_match_ids(player_id: str) -> set[str]:
    async with await _connect() as conn:
        cursor = await conn.execute(
            "SELECT match_id FROM player_matches WHERE player_id = ?", (player_id,))
        rows = await cursor.fetchall()
    return {r["match_id"] for r in rows}


# ── player_scans ──────────────────────────────────────────────────────────────

async def is_player_fully_scanned(player_id: str) -> bool:
    async with await _connect() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM player_scans WHERE player_id = ?", (player_id,))
        row = await cursor.fetchone()
    return row is not None


async def mark_player_scanned(player_id: str, total_matches: int) -> None:
    async with await _connect() as conn:
        await conn.execute("""
            INSERT OR REPLACE INTO player_scans (player_id, scanned_at, total_matches)
            VALUES (?, strftime('%s','now'), ?)
        """, (player_id, total_matches))
        await conn.commit()


# ── player stats cache (3-day TTL) ───────────────────────────────────────────

async def get_cached_player_stats(player_id: str) -> dict | None:
    async with await _connect() as conn:
        cursor = await conn.execute(
            "SELECT stats_json, fetched_at FROM player_stats_cache WHERE player_id = ?",
            (player_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    if int(time.time()) - (row["fetched_at"] or 0) > STATS_TTL_SECONDS:
        return None
    return json.loads(row["stats_json"])


async def save_player_stats(player_id: str, stats: dict) -> None:
    async with await _connect() as conn:
        await conn.execute("""
            INSERT OR REPLACE INTO player_stats_cache (player_id, stats_json, fetched_at)
            VALUES (?, ?, strftime('%s','now'))
        """, (player_id, json.dumps(stats)))
        await conn.commit()


# ── matchroom cache (permanent — roster is immutable) ─────────────────────────

async def get_cached_matchroom(match_id: str) -> dict | None:
    async with await _connect() as conn:
        cursor = await conn.execute(
            "SELECT data_json FROM matchroom_cache WHERE match_id = ?", (match_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    return json.loads(row["data_json"])


async def save_matchroom(match_id: str, data: dict) -> None:
    async with await _connect() as conn:
        await conn.execute("""
            INSERT OR REPLACE INTO matchroom_cache (match_id, data_json, fetched_at)
            VALUES (?, ?, strftime('%s','now'))
        """, (match_id, json.dumps(data)))
        await conn.commit()


async def get_db_stats() -> dict:
    async with await _connect() as conn:
        matches = (await (await conn.execute("SELECT COUNT(*) FROM matches")).fetchone())[0]
        players = (await (await conn.execute("SELECT COUNT(*) FROM players")).fetchone())[0]
        links   = (await (await conn.execute("SELECT COUNT(*) FROM player_matches")).fetchone())[0]
    return {"cached_matches": matches, "known_players": players, "player_match_links": links}
