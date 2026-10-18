# didyoufaceit

CS2 FACEIT match history search, matchroom analyzer, and ban scanner.

Three features:

- **Player vs Player** — find every match two players have shared (as enemies or teammates), with W/L records, map/year/mode filters, and best lineup analysis
- **Matchroom Analyzer** — paste a FACEIT room URL, see each team's maps from best to worst against this opponent, with a rating Diff per map and per-player breakdowns, to help during veto
- **Ban Scanner** — scan a player's match history to find opponents who have since been banned, with ban reason breakdowns and per-match details

## Stack

- **Backend:** Python 3.12+, FastAPI, asyncpg, httpx (async)
- **Frontend:** Alpine.js v3, Tailwind CSS (CDN), multi-page
- **Database:** PostgreSQL 17
- **Deployment:** Docker, Gunicorn + Uvicorn workers

## Quick Start

### With Docker Compose (recommended)

```bash
# Clone and set your API key
cp .env.example .env
# Edit .env with your FACEIT API key

# Run everything
docker compose up --build
```

App runs at `http://localhost:5001`.

### Local Development

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL running locally.

```bash
# Install dependencies
uv sync

# Start postgres (or use docker compose up db -d)
docker compose up db -d

# Run dev server with hot reload
DATABASE_URL=postgresql://didyou:didyou@localhost:5432/didyou \
FACEIT_API_KEY=your_key_here \
PORT=5001 \
uv run didyoufaceit-web
```

## Configuration

All configuration is via environment variables (a `.env` file in the working directory is loaded too; real environment variables win):

| Variable                | Default                                            | Description                                                                 |
| ----------------------- | -------------------------------------------------- | --------------------------------------------------------------------------- |
| `FACEIT_API_KEY`        | _(required)_                                       | Your FACEIT Data API v4 key                                                 |
| `DATABASE_URL`          | `postgresql://didyou:didyou@localhost:5432/didyou` | PostgreSQL connection string                                                |
| `PORT`                  | `5000`                                             | Server port                                                                 |
| `WEB_CONCURRENCY`       | `2`                                                | Gunicorn workers                                                            |
| `FACEIT_MAX_CONCURRENT` | `8`                                                | FACEIT requests in flight per worker, shared by all users of that worker    |
| `MAP_POOL`              | see `src/didyoufaceit/maps.py`                     | Fallback map pool, comma-separated (e.g. `de_ancient,de_anubis,de_cache,…`) |
| `FACEIT_BASE_URL`       | `https://open.faceit.com/data/v4`                  | Point at a mock API for local testing                                       |
| `FACEIT_WEB_FALLBACK`   | `1`                                                | `0` stops asking FACEIT's site for a live room's pool                       |

### Map pool rotation

The matchroom analyzer shows the pool FACEIT sends with each match (`voting.map.entities`), so a FACEIT rotation needs no change here. Players can turn maps off and Vertigo is optional (Season 9), so each room has its own pool, often smaller than the full list; the page shows only that room's maps.

The Data API leaves a live room's voting data empty until the veto is over. For live rooms without it, the server asks FACEIT's site endpoint (`www.faceit.com/api/match/v2/match/{id}`, unofficial, no key, cached 10 seconds per room), which FACEIT's room page loads. Mid-veto it has no `voting` block either, but it lists the room's pool (after players' map preferences) in `matchCustom.tree.map.values.value` and as a `de_…,de_…` tag; the server reads it from there. If that fails too, the page shows the default pool with a short note and re-checks every 15 seconds (`/api/matchroom/pool`) for up to 10 minutes. Set `FACEIT_WEB_FALLBACK=0` to never call the site endpoint. `scripts/probe_live_veto.py` records what both sources return during a real veto. `DEFAULT_POOL` in `src/didyoufaceit/maps.py` (or the `MAP_POOL` variable) is only used for matches that carry no voting data. Player stats are stored for every map, so rotating never requires a cache flush.

## Architecture

### SSE Streaming

All three features stream Server-Sent Events. The server sends `retry: 86400000` first so the browser doesn't reconnect after the page closes a finished stream, and a `: ping` comment after 15 s of silence so proxies keep long steps alive. When the client disconnects, the stream's generator is cancelled and closed, so no further FACEIT calls are made. Errors arrive as `{"type": "error", "status": <http status>, "msg": …}` with no FACEIT response bodies.

### FACEIT API client

One `FaceitClient` per process (created at startup) holds the HTTP connection pool and a semaphore of `FACEIT_MAX_CONCURRENT` requests shared by every user. It retries 429 and 5xx responses and network timeouts up to 4 times with exponential backoff and jitter, honouring `Retry-After`.

### Caching Strategy

| Data                         | Kept                     | Rationale                                                                                   |
| ---------------------------- | ------------------------ | ------------------------------------------------------------------------------------------- |
| Each player's match index    | Permanent                | `player_matches` is what makes a cached search complete; never pruned                       |
| Match rows                   | 1 year                   | Rosters and series score from history; map and round score added from `/stats` when needed |
| Player scan state            | Re-checked after 6 hours | Then only the latest ~300 matches are read                                                  |
| Player lifetime stats        | 3 days                   | Changes slowly                                                                              |
| Player recent matches        | 6 hours                  | Current form for the rating                                                                 |
| Matchroom snapshots          | 7 days, finished only    | Live rooms change during veto, so they're always fetched fresh                              |
| Ban status                   | Adaptive                 | Clean or every ban over: 7 days; active ban: until it ends; permanent: 90 days; failed lookups aren't cached |

Schema changes and one-off data fixes run at startup (`init_db`, recorded in `schema_migrations`), serialized across workers by a Postgres advisory lock. The 6-hourly cleanup runs in one worker at a time.

### Map Diff (Matchroom)

The matchroom shows one column per team, each listing the match's map pool from that team's best map to its worst against this opponent. Each row has the team's Rating, Win % and Matches on the map, plus **Diff**:

- **Rating**: the mean of the five players' map ratings. A player who hasn't played the map counts at their overall rating.
- **Win % / Matches**: lifetime win rate (mean) and total matches, over the players who have played the map.
- **Diff**: this team's rating on the map minus the other team's. The two columns are mirror images: the top of one is the bottom of the other.

The rating approximates HLTV Rating 2.0 (`src/didyoufaceit/rating.py`). FACEIT's own FACEIT Rating is not in the Data API, and HLTV 3.0 needs round-level data, so neither is used. Each player's map rating comes from their last 100 matches (`/players/{id}/games/cs2/stats`), pulled toward their overall recent rating by 8 games; maps they haven't played recently use the midpoint of lifetime and current form. KAST isn't exposed by FACEIT, so it is held at 72%.

A map where either team has no games at all shows "no data" and sorts last. Clicking a map shows its players below, grouped by team (side by side on desktop), each team sorted by rating.

### Search Flow

1. Resolve both nicknames → UUIDs.
2. Walk each player's history, 100 matches per call, streaming progress page by page. History items carry full rosters, so every match is stored as it's read with no per-match calls. Players scanned in the last 6 hours come from cache; older scans read only the latest ~300 matches.
3. Shared matches are the intersection of the two histories.
4. Only those get one `/matches/{id}/stats` call each, for the map and round score (a BO3 shows its maps and the series score). Results stream in batches of 25.

### Matchroom Flow

1. Fetch the match (cached once finished).
2. Fetch all 10 players' lifetime stats (cached 3 days) and last 100 matches (cached 6 hours) concurrently.
3. The browser computes each team's rating and the Diff per map over the match's own veto pool and renders the two columns.

### Ban Scanner Flow

1. Resolve the player.
2. Read one history page (up to 100 matches, paginated by timestamp); rosters, Steam ids and avatars come with it.
3. Check every opponent's bans (cache first). A lookup that fails is skipped, not cached as clean.
4. For banned opponents only, fetch map and score of the matches you played together.

## FACEIT API Notes

- Base URL: `https://open.faceit.com/data/v4`
- Auth: Bearer token
- Player identity: always UUID-first (nicknames are recycled on FACEIT)
- History pagination: max offset 1000, use timestamp pagination (`to` param) for full history
- History: `from` defaults to 0 (all time); items include rosters with Steam ids, series score and timestamps, but not the map
- Player stats: `/players/{id}/stats/cs2` returns lifetime per-map stats with `type:"Map"`, `mode:"5v5"` segments. Segment `K/D Ratio` is a sum of per-match ratios; use `Kills`/`Deaths`
- Recent matches: `/players/{id}/games/cs2/stats` returns one row per match (map, rounds, kills, deaths, assists, ADR, result)
- FACEIT Rating, Round Swing, RWS and KAST are not in the Data API (checked October 2026; re-check with `scripts/probe_faceit_rating.py`)

## Development

```bash
# Lint
uv run ruff check src/
uv run ruff format src/

# Test (needs Postgres: docker compose up db -d)
DATABASE_URL=postgresql://didyou:didyou@localhost:5432/didyou uv run pytest

# Check whether FACEIT exposes new rating fields
FACEIT_API_KEY=... python3 scripts/probe_faceit_rating.py <nickname>

# Build Docker image
docker build -t didyoufaceit .
```

## License

MIT
