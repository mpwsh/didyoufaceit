"""
Inspect FACEIT API responses for matchroom feature development.
Usage:
  uv run python debug_api.py stats <nickname>
  uv run python debug_api.py matchroom <match_id>
"""
import sys, os, json
from src.faceit_finder.api import FaceitClient

api_key = os.environ["FACEIT_API_KEY"]
cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
arg = sys.argv[2] if len(sys.argv) > 2 else "Patitutitu"

with FaceitClient(api_key) as c:
    if cmd == "stats":
        info = c.get_player(arg)
        pid  = info["player_id"]
        print(f"player_id: {pid}")
        print(f"nickname:  {info['nickname']}\n")

        stats = c._get(f"/players/{pid}/stats/cs2")
        print("=== /players/{id}/stats/cs2 ===")
        print(json.dumps(stats, indent=2)[:4000])

    elif cmd == "matchroom":
        match_id = arg
        m = c.get_match(match_id)
        print("=== teams/roster ===")
        for faction, data in m.get("teams", {}).items():
            print(f"\n{faction}: {data.get('name')}")
            for p in data.get("roster", []):
                print(f"  {p['player_id']}  {p.get('nickname')}")
        print("\n=== voting ===")
        print(json.dumps(m.get("voting", {}).get("map"), indent=2))
