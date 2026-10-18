"""Database layer: schema, migrations, cleanup rules, ban TTL."""

import asyncio
import time

import pytest

from didyoufaceit import cache
from didyoufaceit.cache import (
    BAN_CLEAN_TTL_SECONDS,
    BAN_EXPIRY_BUFFER_SECONDS,
    BAN_PERMANENT_TTL_SECONDS,
    _compute_ban_recheck_at,
)

NOW = 1_800_000_000


def iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# ── ban TTL (pure) ────────────────────────────────────────────────────────────


def test_ban_ttl_clean_player():
    assert _compute_ban_recheck_at(False, [], now=NOW) == NOW + BAN_CLEAN_TTL_SECONDS


def test_ban_ttl_expired_ban_is_not_rechecked_every_scan():
    # Used to return a time in the past, so the player was re-fetched on every scan forever
    bans = [{"ends_at": iso(NOW - 400 * 86400)}]
    assert _compute_ban_recheck_at(True, bans, now=NOW) == NOW + BAN_CLEAN_TTL_SECONDS


def test_ban_ttl_active_ban_rechecks_after_it_ends():
    end = NOW + 3 * 86400
    assert _compute_ban_recheck_at(True, [{"ends_at": iso(end)}], now=NOW) == end + BAN_EXPIRY_BUFFER_SECONDS


def test_ban_ttl_permanent_and_far_future():
    assert _compute_ban_recheck_at(True, [{"ends_at": ""}], now=NOW) == NOW + BAN_PERMANENT_TTL_SECONDS
    far = iso(NOW + 5 * 365 * 86400)
    assert _compute_ban_recheck_at(True, [{"ends_at": far}], now=NOW) == NOW + BAN_PERMANENT_TTL_SECONDS


# ── database ──────────────────────────────────────────────────────────────────


async def test_init_db_idempotent_and_migrations_run_once(db):
    await db.execute("INSERT INTO player_scans (player_id, total_matches) VALUES ('p', 5)")
    await cache.init_db()  # second start: migrations already recorded, scan state untouched
    assert await db.fetchval("SELECT count(*) FROM player_scans") == 1
    names = await db.fetch("SELECT name FROM schema_migrations")
    assert len(names) == len(cache._MIGRATIONS)


async def test_cleanup_keeps_history_links(db):
    old = int(time.time()) - cache.MATCH_TTL_SECONDS - 10
    await cache.save_matches_bulk([{"match_id": "m1", "teams": {"faction1": ["a"], "faction2": ["b"]}}])
    await db.execute("UPDATE matches SET fetched_at = $1", old)
    await cache.link_player_matches_bulk("a", ["m1", "m2"])  # m2 never got a row (e.g. search interrupted)
    assert await cache.cleanup_expired()
    assert await db.fetchval("SELECT count(*) FROM matches") == 0
    assert await cache.get_player_match_ids("a") == {"m1", "m2"}


async def test_cleanup_runs_in_one_worker_at_a_time(db):
    async with db.acquire() as other:
        await other.execute("SELECT pg_advisory_lock($1)", cache._CLEANUP_LOCK)
        try:
            assert await cache.cleanup_expired() is False
        finally:
            await other.execute("SELECT pg_advisory_unlock($1)", cache._CLEANUP_LOCK)
    assert await cache.cleanup_expired() is True


async def test_concurrent_init_db_is_serialized(db):
    await asyncio.gather(*(cache.init_db() for _ in range(4)))


async def test_match_details_survive_a_history_upsert(db):
    teams = {"faction1": ["a"], "faction2": ["b"]}
    await cache.save_matches_bulk([{"match_id": "m1", "teams": teams, "score": {"faction1": 1, "faction2": 0}}])
    await cache.save_match_details("m1", "de_nuke", {"faction1": 13, "faction2": 8})
    # The same match read again from history (and twice in one batch) must not wipe the details
    again = {"match_id": "m1", "teams": teams, "score": {"faction1": 1, "faction2": 0}}
    await cache.save_matches_bulk([again, again])
    row = (await cache.get_cached_matches_bulk(["m1"]))["m1"]
    assert row["map"] == "de_nuke"
    assert row["score"] == {"faction1": 13, "faction2": 8}
    assert row["stats_fetched"] is True


async def test_players_upsert_keeps_known_steam_and_avatar(db):
    await cache.upsert_players_bulk([("p", "old", "7656", "https://a")])
    await cache.upsert_players_bulk([("p", "new"), ("p", "newer")])
    assert await cache.get_players(["p"]) == {"p": {"nickname": "newer", "steam_id_64": "7656", "avatar": "https://a"}}
    assert await cache.get_nicknames(["p", "unknown-id"]) == {"p": "newer", "unknown-id": "unknown-"}


async def test_failed_ban_lookup_is_not_cached(db):
    await cache.save_ban_statuses_bulk([("clean", False, [])])
    status = await cache.get_cached_ban_status(["clean", "never-checked"])
    assert status["clean"]["is_banned"] is False
    assert status["never-checked"] is None


@pytest.mark.parametrize("value", ["2024-05-01T10:00:00Z", "2024-05-01T10:00:00+00:00"])
def test_parse_ts(value):
    assert cache.parse_ts(value) == 1714557600
    assert cache.parse_ts("") is None and cache.parse_ts("garbage") is None


async def test_migration_tolerates_non_object_scores(db):
    # Old rows can hold JSON null or arrays; the stats_fetched migration must not crash on them
    await db.execute("DELETE FROM schema_migrations WHERE name = '2026_10_mark_existing_match_details'")
    await db.execute(
        """INSERT INTO matches (match_id, map, score_json) VALUES
           ('a', 'de_nuke', 'null'::jsonb), ('b', 'de_nuke', '[]'::jsonb),
           ('c', 'de_nuke', '{"faction1": 13, "faction2": 8}'), ('d', 'de_nuke', '{"faction1": 1, "faction2": 0}')"""
    )
    await cache.init_db()
    done = {r["match_id"] for r in await db.fetch("SELECT match_id FROM matches WHERE stats_fetched")}
    assert done == {"c"}


async def test_live_matchroom_snapshots_are_not_served(db):
    await cache.save_matchroom("live", {"status": "VOTING", "teams": {}})
    await cache.save_matchroom("over", {"status": "FINISHED", "teams": {}})
    assert await cache.get_cached_matchroom("live") is None
    assert (await cache.get_cached_matchroom("over"))["status"] == "FINISHED"
    # and the one-off migration clears snapshots the old code saved mid-veto
    await db.execute("DELETE FROM schema_migrations WHERE name = '2026_10_drop_live_matchroom_snapshots'")
    await cache.init_db()
    assert await db.fetchval("SELECT count(*) FROM matchroom_cache") == 1


async def test_schema_setup_is_not_bound_by_the_pool_timeout(db, monkeypatch):
    # Another worker holds the schema lock for longer than the pool's command timeout
    await cache.close_db()
    monkeypatch.setattr(cache, "POOL_COMMAND_TIMEOUT", 0.3)
    import asyncpg

    other = await asyncpg.connect(cache.os.environ.get("DATABASE_URL", cache.DEFAULT_DATABASE_URL))
    await other.execute("SELECT pg_advisory_lock($1)", cache._SCHEMA_LOCK)

    async def release():
        await asyncio.sleep(1.0)
        await other.execute("SELECT pg_advisory_unlock($1)", cache._SCHEMA_LOCK)

    try:
        await asyncio.gather(cache.init_db(), release())
    finally:
        await other.close()
