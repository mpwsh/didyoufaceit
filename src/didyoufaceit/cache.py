"""SQLite cache for FACEIT match data and player scan state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


DB_PATH = Path.home() / ".faceit_finder" / "cache.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with _connect() as conn:
        conn.executescript("""
            -- Stable identity: player_id is UUID, nickname is latest known display name
            CREATE TABLE IF NOT EXISTS players (
                player_id   TEXT PRIMARY KEY,
                nickname    TEXT NOT NULL,
                updated_at  INTEGER DEFAULT (strftime('%s','now'))
            );

            -- teams_json stores {faction: [player_id, ...]} — UUIDs only, no nicknames
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

            -- Cached lifetime stats from /players/{id}/stats/cs2
            -- TTL-based: re-fetch after stats_ttl_seconds
            CREATE TABLE IF NOT EXISTS player_stats_cache (
                player_id    TEXT PRIMARY KEY,
                stats_json   TEXT NOT NULL,
                fetched_at   INTEGER DEFAULT (strftime('%s','now'))
            );

            -- Cached matchroom roster+voting (immutable once match created)
            CREATE TABLE IF NOT EXISTS matchroom_cache (
                match_id      TEXT PRIMARY KEY,
                data_json     TEXT NOT NULL,
                fetched_at    INTEGER DEFAULT (strftime('%s','now'))
            );
        """)

        # ── migrations for existing installs ──────────────────────────────────
        match_cols = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
        for col, typedef in [
            ("competition_type", "TEXT"),
            ("competition_name", "TEXT"),
        ]:
            if col not in match_cols:
                conn.execute(f"ALTER TABLE matches ADD COLUMN {col} {typedef}")

        # Drop old teams_ids_json column data if it existed (was transitional)
        # SQLite can't DROP COLUMN before 3.35, so we just ignore it if present

        # player_scans: drop nickname column if present (moved to players table)
        scan_cols = {r[1] for r in conn.execute("PRAGMA table_info(player_scans)")}
        # Can't drop columns in old SQLite — nickname stays but we stop using it


# ── players ───────────────────────────────────────────────────────────────────

def upsert_player(player_id: str, nickname: str) -> None:
    """Store or update a player's latest known nickname."""
    with _connect() as conn:
        conn.execute("""
            INSERT INTO players (player_id, nickname, updated_at)
            VALUES (?, ?, strftime('%s','now'))
            ON CONFLICT(player_id) DO UPDATE SET
                nickname   = excluded.nickname,
                updated_at = excluded.updated_at
        """, (player_id, nickname))


def upsert_players_bulk(players: list[tuple[str, str]]) -> None:
    """Bulk upsert [(player_id, nickname), ...]."""
    with _connect() as conn:
        conn.executemany("""
            INSERT INTO players (player_id, nickname, updated_at)
            VALUES (?, ?, strftime('%s','now'))
            ON CONFLICT(player_id) DO UPDATE SET
                nickname   = excluded.nickname,
                updated_at = excluded.updated_at
        """, players)


def get_nicknames(player_ids: list[str]) -> dict[str, str]:
    """Return {player_id: nickname} for all known ids. Missing ones get a short UUID fallback."""
    if not player_ids:
        return {}
    placeholders = ",".join("?" * len(player_ids))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT player_id, nickname FROM players WHERE player_id IN ({placeholders})",
            player_ids,
        ).fetchall()
    result = {r["player_id"]: r["nickname"] for r in rows}
    # Fallback for any unknown ids
    for pid in player_ids:
        if pid not in result:
            result[pid] = pid[:8]  # short UUID prefix
    return result


# ── matches ───────────────────────────────────────────────────────────────────

def get_cached_match(match_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    if not row:
        return None
    return {
        "match_id":         row["match_id"],
        "started_at":       row["started_at"],
        "status":           row["status"],
        "map":              row["map"],
        "teams":            json.loads(row["teams_json"]),   # {faction: [player_id, ...]}
        "score":            json.loads(row["score_json"]),
        "faceit_url":       row["faceit_url"],
        "competition_type": row["competition_type"] or "",
        "competition_name": row["competition_name"] or "",
    }


def save_match(match_id: str, started_at: int, status: str, map_name: str,
               teams: dict, score: dict, faceit_url: str,
               competition_type: str = "", competition_name: str = "") -> None:
    """teams should be {faction: [player_id, ...]} — UUIDs only."""
    with _connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO matches
              (match_id, started_at, status, map, teams_json, score_json,
               faceit_url, competition_type, competition_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (match_id, started_at, status, map_name,
              json.dumps(teams), json.dumps(score), faceit_url,
              competition_type, competition_name))


def update_match_score(match_id: str, score: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE matches SET score_json = ? WHERE match_id = ?",
            (json.dumps(score), match_id)
        )


# ── player_matches ────────────────────────────────────────────────────────────

def link_player_match(player_id: str, match_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO player_matches (player_id, match_id) VALUES (?, ?)",
            (player_id, match_id)
        )


def link_player_matches_bulk(player_id: str, match_ids: list[str]) -> None:
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO player_matches (player_id, match_id) VALUES (?, ?)",
            [(player_id, mid) for mid in match_ids]
        )


def get_player_match_ids(player_id: str) -> set[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT match_id FROM player_matches WHERE player_id = ?", (player_id,)
        ).fetchall()
    return {r["match_id"] for r in rows}


# ── player_scans ──────────────────────────────────────────────────────────────

def is_player_fully_scanned(player_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM player_scans WHERE player_id = ?", (player_id,)
        ).fetchone()
    return row is not None


def mark_player_scanned(player_id: str, total_matches: int) -> None:
    with _connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO player_scans (player_id, scanned_at, total_matches)
            VALUES (?, strftime('%s','now'), ?)
        """, (player_id, total_matches))


def get_db_stats() -> dict:
    with _connect() as conn:
        matches      = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        players      = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        player_links = conn.execute("SELECT COUNT(*) FROM player_matches").fetchone()[0]
    return {
        "cached_matches":    matches,
        "known_players":     players,
        "player_match_links": player_links,
    }


# ── player stats cache ────────────────────────────────────────────────────────

STATS_TTL_SECONDS = 3 * 24 * 3600  # 3 days


def get_cached_player_stats(player_id: str) -> dict | None:
    """Return cached lifetime stats if fresh (within TTL), else None."""
    import time
    with _connect() as conn:
        row = conn.execute(
            "SELECT stats_json, fetched_at FROM player_stats_cache WHERE player_id = ?",
            (player_id,),
        ).fetchone()
    if not row:
        return None
    age = int(time.time()) - (row["fetched_at"] or 0)
    if age > STATS_TTL_SECONDS:
        return None  # stale
    return json.loads(row["stats_json"])


def save_player_stats(player_id: str, stats: dict) -> None:
    """Cache lifetime stats with current timestamp."""
    with _connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO player_stats_cache (player_id, stats_json, fetched_at)
            VALUES (?, ?, strftime('%s','now'))
        """, (player_id, json.dumps(stats)))


# ── matchroom cache ───────────────────────────────────────────────────────────

def get_cached_matchroom(match_id: str) -> dict | None:
    """Return cached matchroom data (roster + voting). Never expires (immutable)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT data_json FROM matchroom_cache WHERE match_id = ?",
            (match_id,),
        ).fetchone()
    if not row:
        return None
    return json.loads(row["data_json"])


def save_matchroom(match_id: str, data: dict) -> None:
    """Cache matchroom data permanently."""
    with _connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO matchroom_cache (match_id, data_json, fetched_at)
            VALUES (?, ?, strftime('%s','now'))
        """, (match_id, json.dumps(data)))
