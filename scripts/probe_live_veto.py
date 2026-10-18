#!/usr/bin/env python3
"""Watch a live FACEIT room and record when each API exposes its map pool and veto.

Usage:
    FACEIT_API_KEY=... python3 scripts/probe_live_veto.py <room link or match id> [seconds between polls]

Open it as soon as the room appears (accept/check-in), before the veto starts. Every 5 seconds
it asks both sources, prints one line when anything changes, and saves each new response:

- data-api: open.faceit.com/data/v4/matches/{id} (official, needs the key)
- site:     www.faceit.com/api/match/v2/match/{id} (what faceit.com's room page loads, no key)

Stops when the match is over, after 40 minutes, or on Ctrl-C. Responses go to
./probe_out/veto/<match id>/ so they can become test fixtures. The API key is never printed or saved.
Uses only the standard library.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_API = os.environ.get("FACEIT_BASE_URL", "https://open.faceit.com/data/v4") + "/matches/{id}"
SITE = os.environ.get("FACEIT_WEB_MATCH_URL", "https://www.faceit.com/api/match/v2/match/{id}")
USER_AGENT = "didyoufaceit (+https://github.com/mpwsh/didyoufaceit)"  # same as the server
MATCH_ID = re.compile(r"1-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FINAL = {"FINISHED", "CANCELLED", "ABORTED"}
MAX_SECONDS = 40 * 60


def fetch(url: str, key: str | None) -> tuple[int, object]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as r:
            body = r.read().decode(errors="replace")
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, {"_not_json": body[:300]}
    except urllib.error.HTTPError as e:
        return e.code, {"_error": e.read().decode(errors="replace")[:300]}
    except Exception as e:  # noqa: BLE001 - report anything (timeouts, DNS, TLS) and keep polling
        return 0, {"_error": f"{type(e).__name__}: {e}"}


def summarize(code: int, data: object) -> dict:
    """The parts that matter: status, the pool and the veto so far."""
    if not isinstance(data, dict) or code != 200:
        return {"http": code, "error": str((data or {}).get("_error") or (data or {}).get("_not_json") or "")[:120]}
    m = data.get("payload") if isinstance(data.get("payload"), dict) else data
    voting = m.get("voting") or {}
    vmap = voting.get("map") or {}
    pool = [e.get("game_map_id") or e.get("class_name") or e.get("guid") for e in vmap.get("entities") or []]
    extra = sorted(k for k in vmap if k not in ("entities", "pick"))
    # Mid-veto the site has no voting block yet; the room's pool sits in the match tree and a tag
    tree = (((m.get("matchCustom") or {}).get("tree") or {}).get("map") or {}).get("values") or {}
    tree_pool = [e.get("game_map_id") or e.get("guid") for e in tree.get("value") or [] if isinstance(e, dict)]
    tag_pool = next(
        (t.split(",") for t in m.get("tags") or [] if isinstance(t, str) and t.startswith("de_") and "," in t), []
    )
    return {
        "http": code,
        "status": m.get("status"),
        "state": m.get("state"),
        "pool": pool,
        "pick": vmap.get("pick"),
        "tree_pool": tree_pool,
        "tag_pool": tag_pool,
        "other_voting_keys": extra,
        "voting_top_keys": sorted(voting),
    }


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    found = MATCH_ID.search(sys.argv[1].lower())
    if not found:
        sys.exit("That doesn't look like a FACEIT room link or match id (1-xxxxxxxx-…).")
    mid = found.group(0)
    every = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    key = os.environ.get("FACEIT_API_KEY", "")
    if not key:
        print("No FACEIT_API_KEY: only the site endpoint will be checked.")
    out = Path("probe_out") / "veto" / mid
    out.mkdir(parents=True, exist_ok=True)
    timeline: list[dict] = []
    last: dict[str, dict] = {}
    start = time.time()
    print(f"Watching {mid} every {every:g}s. Ctrl-C to stop.\n")
    try:
        while time.time() - start < MAX_SECONDS:
            t = round(time.time() - start)
            statuses = []
            for name, url, k in (("data-api", DATA_API, key), ("site", SITE, None)):
                if name == "data-api" and not key:
                    continue
                code, data = fetch(url.format(id=mid), k)
                s = summarize(code, data)
                statuses.append(s.get("status"))
                if s != last.get(name):
                    last[name] = s
                    timeline.append({"t": t, "source": name, **s})
                    (out / f"{t:05d}_{name}.json").write_text(json.dumps(data, indent=1))
                    pool = s.get("pool")
                    print(
                        f"+{t:>4}s {name:<8} http={s['http']} status={s.get('status')} "
                        f"pool={len(pool) if pool is not None else '-'} {pool or ''} pick={s.get('pick')}"
                        + (f" tree={len(s['tree_pool'])}" if s.get("tree_pool") else "")
                        + (f" tag={len(s['tag_pool'])}" if s.get("tag_pool") else "")
                        + (f" extra={s['other_voting_keys']}" if s.get("other_voting_keys") else "")
                        + (f" error={s['error']}" if s.get("error") else "")
                    )
            if any(str(x).upper() in FINAL for x in statuses):
                print("\nMatch is over.")
                break
            time.sleep(every)
    except KeyboardInterrupt:
        print("\nStopped.")
    (out / "timeline.json").write_text(json.dumps(timeline, indent=1))
    print(f"\nTimeline and responses saved to {out}/")
    print("During VOTING, 'site' should show tree=/tag= with the room's maps while 'data-api' shows pool=0.")


if __name__ == "__main__":
    main()
