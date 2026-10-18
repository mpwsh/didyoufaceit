"""Map pool: normalization, env override, and matchroom pool selection."""

import importlib

import pytest

from didyoufaceit import maps
from didyoufaceit.matchroom import _map_pool_from_match, _parse_map_stats, map_pool_info


@pytest.mark.parametrize(
    "label, expected",
    [
        ("Dust2", "de_dust2"),
        ("de_dust2", "de_dust2"),
        ("Dust 2", "de_dust2"),
        ("Cache", "de_cache"),
        ("de_overpass", "de_overpass"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_map_id(label, expected):
    assert maps.normalize_map_id(label) == expected


def test_default_pool_has_cache_not_overpass():
    assert "de_cache" in maps.DEFAULT_POOL
    assert "de_overpass" not in maps.DEFAULT_POOL
    assert "de_vertigo" in maps.DEFAULT_POOL  # optional since Season 9
    assert len(maps.DEFAULT_POOL) == 8


def test_env_override(monkeypatch):
    monkeypatch.setenv("MAP_POOL", "Dust2, de_mirage,mirage,,Train")
    try:
        reloaded = importlib.reload(maps)
        assert reloaded.ACTIVE_POOL == ("de_dust2", "de_mirage", "de_train")
    finally:
        monkeypatch.delenv("MAP_POOL")
        importlib.reload(maps)


def _segment(label, matches=10, wins=6):
    return {
        "type": "Map",
        "mode": "5v5",
        "label": label,
        "stats": {"Matches": str(matches), "Wins": str(wins), "Kills": "180", "Deaths": "170", "Rounds": "240"},
    }


def test_parse_keeps_every_map():
    parsed = _parse_map_stats([_segment("Cache"), _segment("Overpass"), _segment("Mirage")])
    assert set(parsed) == {"de_cache", "de_overpass", "de_mirage"}
    assert parsed["de_cache"]["wr"] == 60


def test_pool_comes_from_match_voting():
    match = {
        "voting": {
            "map": {
                "entities": [
                    {"game_map_id": "de_cache", "name": "Cache"},
                    {"game_map_id": "de_mirage", "name": "Mirage"},
                ],
                "pick": ["de_cache"],
            }
        }
    }
    pool, names, picked = _map_pool_from_match(match)
    assert pool == ["de_cache", "de_mirage"]
    assert names == {"de_cache": "Cache", "de_mirage": "Mirage"}
    assert picked == "de_cache"


def test_pool_falls_back_to_active_pool():
    pool, names, picked = _map_pool_from_match({"voting": None})
    assert pool == list(maps.ACTIVE_POOL)
    assert names["de_cache"] == "Cache"
    assert picked is None


def test_room_with_six_maps_lists_only_those():
    # A hub or league can run its own pool: no Dust2 here, so the page must not show it
    six = ["de_ancient", "de_anubis", "de_cache", "de_inferno", "de_mirage", "de_nuke"]
    match = {"status": "READY", "voting": {"map": {"entities": [{"game_map_id": m, "guid": m} for m in six]}}}
    info = map_pool_info(match)
    assert info["map_pool"] == six and "de_dust2" not in info["map_pool"]
    assert info["map_pool_source"] == "match"
    assert info["picked_map"] is None and info["match_status"] == "READY"


@pytest.mark.parametrize("voting", [None, {}, {"map": {}}, {"map": {"entities": []}}, {"voted_entity_types": ["map"]}])
def test_room_without_voting_says_the_pool_is_a_guess(voting):
    info = map_pool_info({"status": "VOTING", "voting": voting})
    assert info["map_pool"] == list(maps.ACTIVE_POOL)
    assert info["map_pool_source"] == "default"


class _RoomClient:
    def __init__(self, match, web):
        self.match, self.web, self.web_calls = match, web, 0

    async def get_match(self, match_id):
        return self.match

    async def get_match_web(self, match_id):
        self.web_calls += 1
        return self.web


@pytest.fixture
def no_room_cache(monkeypatch):
    from didyoufaceit import matchroom

    async def none(*_):
        return None

    monkeypatch.setattr(matchroom, "get_cached_matchroom", none)
    monkeypatch.setattr(matchroom, "save_matchroom", none)
    return matchroom


VERTIGO_ROOM = {
    "map": {"entities": [{"game_map_id": m} for m in ("de_vertigo", "de_nuke", "de_cache", "de_mirage", "de_inferno")]}
}


async def test_live_room_takes_the_pool_from_faceits_site(no_room_cache):
    client = _RoomClient({"status": "VOTING", "teams": {}}, {"voting": VERTIGO_ROOM})
    info = map_pool_info(await no_room_cache._fetch_match(client, "1-x"))
    assert (
        info["map_pool_source"] == "match" and "de_vertigo" in info["map_pool"] and "de_dust2" not in info["map_pool"]
    )


@pytest.mark.parametrize(
    "match",
    [
        {"status": "FINISHED", "teams": {}},  # over: the Data API is the record, no fallback
        {"status": "READY", "voting": VERTIGO_ROOM},  # the Data API already has the pool
    ],
)
async def test_site_is_only_asked_when_needed(no_room_cache, match):
    client = _RoomClient(match, {"voting": {"map": {"entities": [{"game_map_id": "de_train"}]}}})
    await no_room_cache._fetch_match(client, "1-x")
    assert client.web_calls == 0


async def test_site_unavailable_falls_back_to_default(no_room_cache):
    client = _RoomClient({"status": "VOTING"}, None)
    info = map_pool_info(await no_room_cache._fetch_match(client, "1-x"))
    assert info["map_pool_source"] == "default" and "de_vertigo" in info["map_pool"]


# A real room mid-veto from FACEIT's site (2026-10-18): state VOTING, no `voting` block yet
def _site_payload():
    import json
    from pathlib import Path

    return json.loads((Path(__file__).parent / "fixtures" / "site_match_voting.json").read_text())["payload"]


ROOM_POOL = ["de_dust2", "de_mirage", "de_nuke", "de_ancient", "de_inferno", "de_anubis", "de_cache"]


def test_site_payload_mid_veto_has_no_voting_block():
    site = _site_payload()
    assert site["state"] == "VOTING" and "voting" not in site


async def test_mid_veto_pool_comes_from_the_match_tree(no_room_cache):
    client = _RoomClient({"status": "VOTING", "teams": {}}, _site_payload())
    info = map_pool_info(await no_room_cache._fetch_match(client, "1-b950c250-7793-4eb8-bede-d18a5cbfd654"))
    assert info["map_pool"] == ROOM_POOL  # this room's players left Vertigo off
    assert info["map_names"]["de_dust2"] == "Dust2"
    assert info["map_pool_source"] == "match" and info["picked_map"] is None


def test_mid_veto_pool_from_the_tag_when_the_tree_is_missing():
    from didyoufaceit.matchroom import _site_voting

    site = _site_payload()
    site.pop("matchCustom")
    ids = [e["game_map_id"] for e in _site_voting(site)["map"]["entities"]]
    assert ids == ["de_dust2", "de_nuke", "de_inferno", "de_mirage", "de_ancient", "de_anubis", "de_cache"]


def test_site_payload_without_any_pool_is_none():
    from didyoufaceit.matchroom import _site_voting

    site = _site_payload()
    site.pop("matchCustom")
    site["tags"] = ["nonveteran", "Peru", "match_0"]
    assert _site_voting(site) is None


def test_finished_match_result_from_the_real_fixture():
    import json
    from pathlib import Path

    from didyoufaceit.matchroom import match_result

    match = json.loads((Path(__file__).parent / "fixtures" / "match.json").read_text())
    assert match_result(match) == {"winner": "faction2", "score": {"faction2": 13, "faction1": 8}}


@pytest.mark.parametrize(
    "match",
    [
        {"status": "READY", "results": {"winner": "faction1", "score": {"faction1": 1, "faction2": 0}}},
        {"status": "CANCELLED", "results": {}},
        {"status": "FINISHED"},
        {"status": "FINISHED", "results": {"score": {"faction1": "x", "faction2": 3}}},
    ],
)
def test_no_result_unless_finished_with_one(match):
    from didyoufaceit.matchroom import match_result

    assert match_result(match) is None
