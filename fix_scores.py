"""
Fix cached matches that have series scores (0/1, 1/0) instead of real round scores.
Fetches /matches/{id}/stats for each bad match and updates the DB.

Usage:  uv run python fix_scores.py [--dry-run]
"""
import sys, os, json, time
from src.faceit_finder.cache import _connect, save_match
from src.faceit_finder.api import FaceitClient
from src.faceit_finder.search import _round_score_from_stats

api_key  = os.environ["FACEIT_API_KEY"]
dry_run  = "--dry-run" in sys.argv

def get_bad_matches(conn):
    rows = conn.execute("SELECT * FROM matches").fetchall()
    bad = []
    for r in rows:
        score = json.loads(r["score_json"])
        vals  = list(score.values())
        if vals and max(vals) <= 1:
            bad.append(dict(r))
    return bad

with _connect() as conn:
    bad = get_bad_matches(conn)

print(f"Found {len(bad)} matches with series scores (max rounds ≤ 1)")
if not bad:
    print("Nothing to fix.")
    sys.exit(0)

if dry_run:
    print("Dry run — no changes will be made.")
    for m in bad[:10]:
        print(f"  {m['match_id']}  score={m['score_json']}")
    sys.exit(0)

fixed = 0
failed = 0

with FaceitClient(api_key) as client:
    for i, m in enumerate(bad):
        match_id    = m["match_id"]
        old_score   = json.loads(m["score_json"])
        teams       = json.loads(m["teams_json"])

        try:
            stats       = client.get_match_stats(match_id)
            round_score = _round_score_from_stats(stats, old_score)

            if round_score and max(round_score.values()) > 1:
                save_match(
                    match_id         = match_id,
                    started_at       = m["started_at"],
                    status           = m["status"],
                    map_name         = m["map"],
                    teams            = teams,
                    score            = round_score,
                    faceit_url       = m["faceit_url"],
                    competition_type = m["competition_type"] or "",
                    competition_name = m["competition_name"] or "",
                )
                print(f"[{i+1}/{len(bad)}] ✓  {match_id}  {old_score} → {round_score}")
                fixed += 1
            else:
                print(f"[{i+1}/{len(bad)}] ?  {match_id}  stats gave {round_score} — skipping")
                failed += 1

        except Exception as e:
            print(f"[{i+1}/{len(bad)}] ✗  {match_id}  {e}")
            failed += 1

        # be polite to the API
        time.sleep(0.15)

print(f"\nDone. Fixed: {fixed}  Skipped/failed: {failed}")