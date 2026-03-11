"""
Debug a specific match — dumps raw API fields to understand score issues.
Usage:  uv run python debug_match.py <match_id>
"""
import sys, os, json
from src.faceit_finder.api import FaceitClient

MATCH_ID = sys.argv[1] if len(sys.argv) > 1 else "1-f5558448-1d04-4756-8a74-951030157c1c"
api_key  = os.environ["FACEIT_API_KEY"]

with FaceitClient(api_key) as c:
    print(f"\n{'='*60}")
    print(f"MATCH  {MATCH_ID}")
    print(f"{'='*60}\n")

    # ── /matches/{id} ─────────────────────────────────────────
    m = c.get_match(MATCH_ID)

    print("── results ──")
    print(json.dumps(m.get("results"), indent=2))

    print("\n── teams (roster keys) ──")
    for team, data in m.get("teams", {}).items():
        nicks = [p.get("nickname") for p in data.get("roster", [])]
        print(f"  {team}: {nicks}")

    print("\n── voting/map ──")
    print(json.dumps(m.get("voting", {}).get("map"), indent=2))

    print("\n── status / competition_type ──")
    print(f"  status           : {m.get('status')}")
    print(f"  competition_type : {m.get('competition_type')}")

    # ── /matches/{id}/stats ───────────────────────────────────
    print(f"\n── /stats ──")
    try:
        stats = c.get_match_stats(MATCH_ID)
        rounds = stats.get("rounds", [])
        print(f"  rounds count: {len(rounds)}")
        if rounds:
            r0 = rounds[0]
            print(f"  round[0] teams:")
            for t in r0.get("teams", []):
                ts = t.get("team_stats", {})
                print(f"    team_id={t.get('team_id')}  Final Score={ts.get('Final Score')}  all keys={list(ts.keys())[:8]}")
    except Exception as e:
        print(f"  stats error: {e}")