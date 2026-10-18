"""Shared fixtures: a clean database per test and a fake FACEIT API built from history items."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from didyoufaceit import cache
from didyoufaceit.api import FaceitAPIError

FIX = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIX / name).read_text())


@pytest.fixture
async def db():
    """Schema created, every table emptied; skipped when no Postgres is reachable."""
    try:
        await cache.init_db()
    except (OSError, Exception) as e:  # noqa: BLE001 - any connection failure means "no database here"
        await cache.close_db()
        pytest.skip(f"Postgres not available: {e}")
    pool = await cache._get_pool()
    await pool.execute(
        "TRUNCATE players, matches, player_matches, player_scans, player_stats_cache, "
        "player_recent_cache, matchroom_cache, player_bans"
    )
    yield pool
    await cache.close_db()


def history_item(mid: str, started: int, team1: list[str], team2: list[str], winner: str = "faction1") -> dict:
    """A /players/{id}/history item shaped like the real one (tests/fixtures/history.json)."""

    def team(pids):
        return {
            "team_id": pids[0],
            "nickname": f"team_{pids[0]}",
            "players": [
                {"player_id": p, "nickname": f"nick_{p}", "game_player_id": f"7656{p}", "avatar": f"https://a/{p}.jpg"}
                for p in pids
            ],
        }

    return {
        "match_id": mid,
        "game_id": "cs2",
        "game_mode": "5v5",
        "status": "finished",
        "started_at": started,
        "finished_at": started + 2000,
        "competition_type": "matchmaking",
        "competition_name": "Europe 5v5 Queue",
        "faceit_url": f"https://www.faceit.com/{{lang}}/cs2/room/{mid}",
        "teams": {"faction1": team(team1), "faction2": team(team2)},
        "results": {
            "winner": winner,
            "score": {"faction1": 1 if winner == "faction1" else 0, "faction2": 0 if winner == "faction1" else 1},
        },
    }


class FakeFaceit:
    """Serves history pages, match stats and bans from an in-memory list of history items."""

    def __init__(self, items: list[dict], bans: dict[str, list] | None = None):
        self.items = sorted(items, key=lambda i: -i["started_at"])
        self.by_id = {i["match_id"]: i for i in items}
        self.bans = bans or {}
        self.calls: list[str] = []

    def _players(self, item):
        return [p["player_id"] for t in item["teams"].values() for p in t["players"]]

    async def get_player(self, nickname: str) -> dict:
        self.calls.append("player")
        if not nickname.startswith("nick_"):
            raise FaceitAPIError(404, "/players", "not found")
        pid = nickname.removeprefix("nick_")
        return {"player_id": pid, "nickname": nickname, "steam_id_64": "", "avatar": ""}

    async def get_player_by_id(self, player_id: str) -> dict:
        self.calls.append("player_by_id")
        return {"player_id": player_id, "nickname": f"nick_{player_id}", "steam_id_64": "x", "avatar": ""}

    async def get_match_history(self, player_id, game="cs2", offset=0, limit=100, to=None):
        self.calls.append("history")
        mine = [i for i in self.items if player_id in self._players(i) and (to is None or i["started_at"] <= to)]
        return {"items": mine[offset : offset + limit]}

    async def get_match(self, match_id):
        self.calls.append("match")
        raise AssertionError("search and ban scan should not need /matches/{id} when history has the match")

    async def get_match_stats(self, match_id):
        self.calls.append("stats")
        item = self.by_id[match_id]
        f1, f2 = item["teams"]["faction1"], item["teams"]["faction2"]
        win1 = item["results"]["winner"] == "faction1"
        return {
            "rounds": [
                {
                    "round_stats": {"Map": "de_mirage"},
                    "teams": [
                        {
                            "team_id": f1["team_id"],
                            "team_stats": {"Final Score": "13" if win1 else "9"},
                            "players": f1["players"],
                        },
                        {
                            "team_id": f2["team_id"],
                            "team_stats": {"Final Score": "9" if win1 else "13"},
                            "players": f2["players"],
                        },
                    ],
                }
            ]
        }

    async def get_player_bans(self, player_id):
        self.calls.append("bans")
        result = self.bans.get(player_id, [])
        if isinstance(result, Exception):
            raise result
        return result

    def count(self, kind: str) -> int:
        return self.calls.count(kind)
