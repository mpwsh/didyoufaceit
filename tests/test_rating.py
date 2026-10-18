"""Rating and matchroom stats against real FACEIT responses (tests/fixtures, recorded 2026-10-18)."""

import json
from pathlib import Path

import pytest

from didyoufaceit.matchroom import _map_pool_from_match, _merge_player_stats, _parse_map_stats
from didyoufaceit.rating import KAST_CONSTANT, hltv2_approx, recent_form, shrink

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def segments():
    return load("player_stats_cs2.json")["segments"]


@pytest.fixture
def recent_rows():
    return [item["stats"] for item in load("player_games_stats.json")["items"]]


def test_hltv2_formula_by_hand():
    # 0.70 KPR, 0.70 DPR, 0.14 APR, 75 ADR, flat 72% KAST
    kpr, dpr, apr, adr = 0.70, 0.70, 0.14, 75
    impact = 2.13 * kpr + 0.42 * apr - 0.41
    expected = 0.0073 * KAST_CONSTANT + 0.3591 * kpr - 0.5329 * dpr + 0.2372 * impact + 0.0032 * adr + 0.1587
    assert hltv2_approx(70, 70, 14, 100, 75) == pytest.approx(expected)
    assert hltv2_approx(70, 70, 14, 100, 75) == pytest.approx(1.07, abs=0.01)
    assert hltv2_approx(10, 10, 2, 0, 80) == 0.0


def test_shrink():
    assert shrink(1.30, 0, 1.0) == 1.0
    assert shrink(1.30, 8, 1.0, k=8) == pytest.approx(1.15)


def test_lifetime_segments_5v5_only(segments):
    stats = _parse_map_stats(segments)
    # Wingman Nuke and the 1v1 aim map are ignored; retired maps are kept (the UI filters by pool)
    assert set(stats) == {"de_nuke", "de_overpass", "de_mirage", "de_inferno"}
    nuke = stats["de_nuke"]
    assert (nuke["matches"], nuke["wins"], nuke["wr"]) == (607, 335, 55)
    # K/D from totals (9345 / 8944), not the segment's summed "K/D Ratio" (690.33)
    assert nuke["kd"] == pytest.approx(1.04, abs=0.01)
    assert nuke["rating"] == pytest.approx(round(hltv2_approx(9345, 8944, 3377, 13171, 78.09), 2))
    assert nuke["rating"] == pytest.approx(1.11, abs=0.01)


def test_recent_form_pools_by_map(recent_rows):
    form = recent_form(recent_rows)
    overall = form["overall"]
    assert (overall["matches"], overall["wins"]) == (5, 3)
    # 86 kills, 75 deaths, 31 assists, 110 rounds, 8,733 damage over the 5 matches
    assert overall["rating"] == pytest.approx(round(hltv2_approx(86, 75, 31, 110, 8733 / 110), 2))
    nuke = form["maps"]["de_nuke"]
    assert (nuke["matches"], nuke["wins"]) == (2, 1)
    assert nuke["kd"] == pytest.approx(35 / 31, abs=0.01)
    assert nuke["adr"] == pytest.approx(3705 / 45, abs=0.1)
    # Two games barely move the number off the player's overall form
    assert nuke["rating"] == pytest.approx(shrink(nuke["rating_raw"], 2, overall["rating"]), abs=0.01)
    assert abs(nuke["rating"] - overall["rating"]) < abs(nuke["rating_raw"] - overall["rating"]) + 1e-9


def test_merge_uses_recent_rating_and_lifetime_counts(segments, recent_rows):
    merged = _merge_player_stats(segments, recent_rows)
    ms = merged["map_stats"]
    assert merged["overall_rating"] == recent_form(recent_rows)["overall"]["rating"]
    # Nuke: lifetime matches and win rate, recent rating
    assert (ms["de_nuke"]["matches"], ms["de_nuke"]["wr"]) == (607, 55)
    assert ms["de_nuke"]["recent_matches"] == 2
    assert ms["de_nuke"]["rating"] == recent_form(recent_rows)["maps"]["de_nuke"]["rating"]
    # Mirage: not played recently, so halfway between lifetime and current form
    mirage = ms["de_mirage"]
    assert mirage["recent_matches"] == 0
    assert mirage["rating"] == pytest.approx((mirage["rating_lifetime"] + merged["overall_rating"]) / 2, abs=0.01)
    # Ancient and Anubis only exist in the recent rows of this trimmed fixture
    assert ms["de_ancient"]["matches"] == 1 and ms["de_ancient"]["wr"] == 100


def test_merge_without_recent_falls_back_to_lifetime(segments):
    merged = _merge_player_stats(segments, None)
    assert merged["map_stats"]["de_nuke"]["rating"] == merged["map_stats"]["de_nuke"]["rating_lifetime"]
    assert merged["overall_rating"] is not None


def test_real_match_pool_is_the_veto_not_the_active_duty_list():
    pool, names, picked = _map_pool_from_match(load("match.json"))
    # This SA queue match vetoed 5 maps, including Cache
    assert pool == ["de_cache", "de_anubis", "de_nuke", "de_ancient", "de_inferno"]
    assert names["de_cache"] == "Cache"
    assert picked == "de_nuke"


class _FakeClient:
    def __init__(self, fail_recent=False):
        self.calls = []
        self.fail_recent = fail_recent

    async def get_player_stats(self, pid):
        self.calls.append("lifetime")
        return load("player_stats_cs2.json")

    async def get_player_match_stats(self, pid, offset=0, limit=100):
        self.calls.append(f"recent:{limit}")
        if self.fail_recent:
            raise RuntimeError("timeout")
        return load("player_games_stats.json")


@pytest.fixture
def no_db(monkeypatch):
    """Empty cache, writes dropped: exercises the fetch path without Postgres."""
    from didyoufaceit import matchroom

    async def none(*_):
        return None

    for name in ("get_cached_player_stats", "get_cached_player_recent", "save_player_stats", "save_player_recent"):
        monkeypatch.setattr(matchroom, name, none)
    return matchroom


async def test_fetch_player_stats_two_calls(no_db):
    client = _FakeClient()
    out = await no_db._fetch_player_stats(client, "pid", "nick")
    assert sorted(client.calls) == ["lifetime", "recent:100"]
    assert out["source"] == "recent"
    assert out["map_stats"]["de_nuke"]["recent_matches"] == 2
    assert out["overall_rating"] > 0


async def test_fetch_player_stats_survives_recent_failure(no_db):
    out = await no_db._fetch_player_stats(_FakeClient(fail_recent=True), "pid", "nick")
    assert out["source"] == "lifetime"
    assert out["map_stats"]["de_nuke"]["rating"] == out["map_stats"]["de_nuke"]["rating_lifetime"]
