"""Search and ban scan end to end against a fake FACEIT API and a real Postgres."""

import time

from conftest import FakeFaceit, history_item, load_fixture

from didyoufaceit import cache
from didyoufaceit.api import FaceitAPIError
from didyoufaceit.banscan import stream_ban_scan
from didyoufaceit.matches import details_from_stats, match_from_history_item, score_str
from didyoufaceit.search import stream_search

T0 = 1_790_000_000


async def collect(gen):
    return [ev async for ev in gen]


# ── conversions on real payloads (no database) ────────────────────────────────


def test_history_item_from_real_payload():
    item = load_fixture("history.json")["items"][0]
    match, players = match_from_history_item(item)
    assert match["match_id"] == "1-933a200e-5a1a-43a9-b9d5-d1fe703acaca"
    assert match["status"] == "finished"
    assert match["score"] == {"faction2": 13, "faction1": 8}  # history carries the series score
    assert "4e11d17b-ca6d-46ab-b988-bc145d64481d" in match["teams"]["faction2"]
    assert match["faceit_url"].startswith("https://www.faceit.com/en/")
    bau = next(p for p in players if p[1] == "_bau")
    assert bau[2] == "76561198001570573"  # Steam id comes with history, no profile call needed


def test_stats_details_from_real_payload():
    item = load_fixture("history.json")["items"][0]
    match, _ = match_from_history_item(item)
    map_name, score = details_from_stats(load_fixture("match_stats.json"), match["teams"])
    assert map_name == "de_nuke"
    assert score == {"faction1": 8, "faction2": 13}
    assert score_str(score, "faction2") == "13 / 8"


def test_bo3_keeps_series_score():
    teams = {"faction1": ["a"], "faction2": ["b"]}

    def rnd(m, win_a):
        return {
            "round_stats": {"Map": m},
            "teams": [
                {
                    "team_stats": {"Team Win": "1" if win_a else "0", "Final Score": "13"},
                    "players": [{"player_id": "a"}],
                },
                {
                    "team_stats": {"Team Win": "0" if win_a else "1", "Final Score": "7"},
                    "players": [{"player_id": "b"}],
                },
            ],
        }

    stats = {"rounds": [rnd("de_nuke", True), rnd("de_inferno", False), rnd("de_ancient", True)]}
    assert details_from_stats(stats, teams) == ("de_nuke / de_inferno / de_ancient", {"faction1": 2, "faction2": 1})


# ── head-to-head search ───────────────────────────────────────────────────────


def _histories():
    """You (y) play 250 matches; the opponent (o) is in 30 of them, 10 on your team; plus 40 alone."""
    items = []
    for n in range(250):
        if n < 20:
            t1, t2 = ["y", "a1", "a2", "a3", "a4"], ["o", "b1", "b2", "b3", "b4"]
        elif n < 30:
            t1, t2 = ["y", "o", "a2", "a3", "a4"], ["b0", "b1", "b2", "b3", "b4"]
        else:
            t1, t2 = ["y", "a1", "a2", "a3", "a4"], ["c0", "c1", "c2", "c3", "c4"]
        items.append(history_item(f"m{n:04d}", T0 - n * 3600, t1, t2, winner="faction1" if n % 3 else "faction2"))
    for n in range(40):
        items.append(
            history_item(f"x{n:04d}", T0 - n * 3700, ["o", "d1", "d2", "d3", "d4"], ["e0", "e1", "e2", "e3", "e4"])
        )
    return items


async def test_search_finds_shared_matches_with_one_stats_call_each(db):
    api = FakeFaceit(_histories())
    events = await collect(stream_search(api, "nick_y", "nick_o"))
    matches = [e for e in events if e["type"] == "match"]
    done = events[-1]

    assert events[0]["type"] == "players_resolved"
    assert len(matches) == 30 and done["total_found"] == 30
    assert sum(m["same_team"] for m in matches) == 10
    assert done["total_checked"] == 30  # only the intersection is examined
    assert api.count("match") == 0  # rosters came from history
    assert api.count("stats") == 30  # one per shared match, for map and round score
    assert api.count("history") == 3 + 1  # 250 matches = 3 pages; 70 = 1 page
    first = next(m for m in matches if m["match_id"] == "m0001")
    assert first["map"] == "de_mirage" and first["score"] == "13 / 9"
    assert first["teams"][first["your_team"]][0] == "nick_y"

    # Progress arrives while history is still being read, not all at the end
    statuses = [i for i, e in enumerate(events) if e["type"] == "status" and "Fetching history" in e["msg"]]
    assert len(statuses) >= 4


async def test_repeat_search_is_served_from_cache(db):
    api = FakeFaceit(_histories())
    await collect(stream_search(api, "nick_y", "nick_o"))
    api.calls.clear()
    events = await collect(stream_search(api, "nick_y", "nick_o"))
    assert api.calls == ["player", "player"]  # nicknames only; no history, match or stats calls
    assert events[-1]["cached"] is True and events[-1]["total_found"] == 30


async def test_interrupted_scan_does_not_lose_history(db):
    api = FakeFaceit(_histories())
    gen = stream_search(api, "nick_y", "nick_o")
    async for ev in gen:  # stop after the first history page, like a closed tab
        if ev["type"] == "status" and "(100 so far)" in ev["msg"]:
            break
    await gen.aclose()
    assert await cache.get_player_scan_info("y") is None  # not marked complete
    await cache.cleanup_expired()  # the old cleanup deleted these links within 6 hours
    assert len(await cache.get_player_match_ids("y")) == 100
    events = await collect(stream_search(api, "nick_y", "nick_o"))
    assert events[-1]["total_found"] == 30


async def test_search_errors(db):
    api = FakeFaceit(_histories())
    events = await collect(stream_search(api, "missing", "nick_o"))
    assert events == [{"type": "error", "status": 404, "msg": "Player 'missing' not found"}]
    events = await collect(stream_search(api, "nick_y", "nick_y"))
    assert events[-1]["status"] == 400


async def test_stale_scan_reads_only_new_pages(db):
    api = FakeFaceit(_histories())
    await collect(stream_search(api, "nick_y", "nick_o"))
    await db.execute("UPDATE player_scans SET scanned_at = $1", int(time.time()) - 7 * 3600)
    api.calls.clear()
    await collect(stream_search(api, "nick_y", "nick_o"))
    assert api.count("history") == 2  # one page each shows nothing new: stop
    assert api.count("stats") == 0


async def test_stale_scan_closes_a_gap_longer_than_a_few_pages(db):
    items = _histories()
    api = FakeFaceit(items)
    await collect(stream_search(api, "nick_y", "nick_o"))
    await db.execute("UPDATE player_scans SET scanned_at = $1", int(time.time()) - 7 * 3600)
    # 450 new matches since, shared match #400 among them: deeper than the old 3-page refresh
    for n in range(450):
        t2 = ["o", "b1", "b2", "b3", "b4"] if n == 400 else ["f0", "f1", "f2", "f3", "f4"]
        items.append(history_item(f"n{n:04d}", T0 + (450 - n) * 3600, ["y", "a1", "a2", "a3", "a4"], t2))
    api2 = FakeFaceit(items)
    events = await collect(stream_search(api2, "nick_y", "nick_o"))
    assert events[-1]["total_found"] == 31
    assert any(e.get("match_id") == "n0400" for e in events)
    assert api2.count("history") <= 5 + 2


async def test_stats_not_published_yet_are_asked_again(db):
    recent = int(time.time()) - 600
    item = history_item("fresh", recent, ["y", "a1", "a2", "a3", "a4"], ["o", "b1", "b2", "b3", "b4"])
    old = history_item("old", recent - 3 * 86400, ["y", "a1", "a2", "a3", "a4"], ["o", "b1", "b2", "b3", "b4"])
    api = FakeFaceit([item, old])

    async def no_stats(mid):
        api.calls.append("stats")
        raise FaceitAPIError(404, f"/matches/{mid}/stats", "")

    api.get_match_stats = no_stats
    await collect(stream_search(api, "nick_y", "nick_o"))
    rows = await cache.get_cached_matches_bulk(["fresh", "old"])
    assert rows["fresh"]["stats_fetched"] is False  # just finished: try again next search
    assert rows["old"]["stats_fetched"] is True  # three days old: stats are never coming


# ── ban scan ──────────────────────────────────────────────────────────────────


def _ban(start_ts: int, end_ts: int | None = None, reason="cheating"):
    def iso(ts):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))

    return {
        "nickname": "x",
        "type": "ban",
        "reason": reason,
        "starts_at": iso(start_ts),
        "ends_at": iso(end_ts) if end_ts else "",
    }


async def test_ban_scan_flags_bans_after_the_match_and_skips_failed_lookups(db):
    items = [
        history_item(f"m{n}", T0 - n * 3600, ["y", "a1", "a2", "a3", "a4"], [f"c{n}", "b1", "b2", "b3", "cheater"])
        for n in range(5)
    ]
    api = FakeFaceit(
        items,
        bans={
            "cheater": [_ban(T0 + 86400)],
            "b1": [_ban(T0 - 400 * 86400, T0 - 399 * 86400, reason="afk")],  # old cooldown, long over
            "b2": FaceitAPIError(503, "/players/b2/bans", "busy"),
        },
    )
    events = await collect(stream_ban_scan(api, "nick_y"))
    banned = {e["player_id"]: e for e in events if e["type"] == "banned_player"}

    assert set(banned) == {"cheater", "b1"}
    assert "after_match" not in banned["cheater"]["bans"][0]
    assert banned["cheater"]["matches_against"] == 5
    assert banned["cheater"]["matches"][0]["map"] == "de_mirage"  # details fetched for these matches only
    assert banned["cheater"]["steam_id_64"].startswith("7656")  # from history, no profile call
    assert api.count("match") == 0 and api.count("player_by_id") == 0
    assert api.count("stats") == 5  # the five matches against banned players

    cached = await cache.get_cached_ban_status(["b2", "b1", "b3", "a1"])
    assert cached["b2"] is None  # the failed lookup is retried next time, not cached as clean
    assert cached["b3"]["is_banned"] is False
    assert cached["a1"] is None  # teammates are not opponents
    # b1's ban is long over: re-checked in a week, not on every scan
    recheck = await db.fetchval("SELECT recheck_at FROM player_bans WHERE player_id = 'b1'")
    assert recheck > time.time() + 6 * 86400

    api.calls.clear()
    events = await collect(stream_ban_scan(api, "nick_y"))
    assert api.count("bans") == 1  # only b2 again
    assert events[-1]["banned_count"] == 2
