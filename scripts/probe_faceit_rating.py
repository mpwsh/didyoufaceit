#!/usr/bin/env python3
"""Check whether the FACEIT Data API exposes FACEIT Rating, Round Swing, RWS or KAST.

Usage:
    FACEIT_API_KEY=... python3 scripts/probe_faceit_rating.py <nickname> [match_id]

Uses only the standard library. Makes about 6 API calls. Raw responses are saved to
./probe_out/ so they can double as test fixtures. The API key is never printed or saved.

If no match_id is given, the script picks the player's most recent finished CS2 match
played after FACEIT Rating launched (Season 8, 22 April 2026).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("FACEIT_BASE_URL", "https://open.faceit.com/data/v4")
SEASON8_START = 1776816000  # 2026-04-22 00:00 UTC
INTERESTING = re.compile(r"rating|swing|rws|kast|impact|win.?prob", re.IGNORECASE)
OUT = Path("probe_out")


def get(path: str, key: str, **params) -> tuple[int, object]:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BASE}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        return e.code, {"_error": body}
    except urllib.error.URLError as e:
        return 0, {"_error": str(e.reason)}


def save(name: str, data: object) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))


def find_keys(obj: object, prefix: str = "") -> list[tuple[str, object]]:
    """Every (path, value) whose key name looks like a rating / swing / KAST field."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            # teams.factionN.stats.{rating,winProbability} are team Elo and the pre-match win chance
            team_elo = re.match(r"^teams\.faction\d+\.stats\.", path)
            if INTERESTING.search(str(k)) and not isinstance(v, dict | list) and not team_elo:
                hits.append((path, v))
            hits.extend(find_keys(v, path))
    elif isinstance(obj, list) and obj:
        hits.extend(find_keys(obj[0], f"{prefix}[0]"))  # first item is enough to learn the shape
    return hits


def report(title: str, status: int, data: object, stat_keys: list[str] | None = None) -> list[tuple[str, object]]:
    print(f"\n== {title}  (HTTP {status})")
    if status != 200:
        print(f"   not available: {data.get('_error', '') if isinstance(data, dict) else data}")
        return []
    if stat_keys is not None:
        print(f"   {len(stat_keys)} stat keys: {', '.join(sorted(stat_keys))}")
    hits = find_keys(data)
    if hits:
        for path, value in hits:
            print(f"   FOUND  {path} = {value!r}")
    else:
        print("   no rating / swing / RWS / KAST fields")
    return hits


def main() -> int:
    key = os.environ.get("FACEIT_API_KEY", "").strip()
    if not key or len(sys.argv) < 2:
        print(__doc__)
        return 2
    nickname = sys.argv[1]
    match_id = sys.argv[2] if len(sys.argv) > 2 else None
    found: dict[str, list] = {}

    # 1. Player
    status, player = get("/players", key, nickname=nickname)
    if status != 200:
        print(f"Could not resolve {nickname!r}: HTTP {status} {player}")
        return 1
    pid = player["player_id"]
    print(f"Player {player.get('nickname')} ({pid})")

    # 2. Pick a post-Season-8 match
    if not match_id:
        status, history = get(f"/players/{pid}/history", key, game="cs2", offset=0, limit=20)
        save("history", history)
        items = history.get("items", []) if status == 200 else []
        recent = [m for m in items if (m.get("started_at") or 0) >= SEASON8_START and m.get("status") == "finished"]
        if not recent:
            print("No finished CS2 match since 22 April 2026 in the last 20; pass a match_id instead.")
            return 1
        match_id = recent[0]["match_id"]
        print("History item keys:", ", ".join(sorted(recent[0])))
    print(f"Match {match_id}")

    # 3. Per-match stats: the most likely home for a per-match rating
    status, mstats = get(f"/matches/{match_id}/stats", key)
    save("match_stats", mstats)
    keys = None
    if status == 200:
        for rnd in mstats.get("rounds", []):
            for team in rnd.get("teams", []):
                for p in team.get("players", []):
                    if p.get("player_id") == pid:
                        keys = list(p.get("player_stats", {}))
                        print("   your player_stats:", json.dumps(p.get("player_stats", {}), ensure_ascii=False))
    found["/matches/{id}/stats"] = report("GET /matches/{id}/stats", status, mstats, keys)

    # 4. Match details (scoreboard extras sometimes live here)
    status, match = get(f"/matches/{match_id}", key)
    save("match", match)
    found["/matches/{id}"] = report("GET /matches/{id}", status, match)

    # 5. Lifetime + per-map segments (what the matchroom uses today)
    status, life = get(f"/players/{pid}/stats/cs2", key)
    save("player_stats_cs2", life)
    seg = next((s for s in life.get("segments", []) if s.get("type") == "Map"), {}) if status == 200 else {}
    keys = list(life.get("lifetime", {})) + [f"segment:{k}" for k in seg.get("stats", {})] if status == 200 else None
    found["/players/{id}/stats/cs2"] = report("GET /players/{id}/stats/cs2", status, life, keys)

    # 6. Per-match stats list for one player (would let Edge average recent matches cheaply)
    status, games = get(f"/players/{pid}/games/cs2/stats", key, offset=0, limit=5)
    save("player_games_stats", games)
    first = (games.get("items") or [{}])[0] if status == 200 else {}
    keys = list(first.get("stats", first)) if status == 200 else None
    found["/players/{id}/games/cs2/stats"] = report("GET /players/{id}/games/cs2/stats", status, games, keys)

    # Verdict
    print("\n== Verdict")
    any_hit = False
    for endpoint, hits in found.items():
        if hits:
            any_hit = True
            names = sorted({p.split(".")[-1] for p, _ in hits})
            print(f"   {endpoint}: {', '.join(names)}")
    if not any_hit:
        print("   FACEIT Rating is not exposed by these endpoints. Keep the HLTV 2.0 approximation.")
    else:
        print("   Exposed. Send me the probe_out/ folder and I'll wire Edge to it.")
    print(f"\nRaw responses saved in {OUT.resolve()}/ (no API key inside).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
