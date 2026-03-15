# didyoufaceit

CS2 FACEIT match history search, matchroom analyzer, and ban scanner.

Three features:

- **Player vs Player** — find every match two players have shared (as enemies or teammates), with W/L records, map/year/mode filters, and best lineup analysis
- **Matchroom Analyzer** — paste a FACEIT room URL, get per-player map breakdowns with Edge scoring to help decide which map to pick or ban during veto
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

All configuration is via environment variables:

| Variable         | Default                                            | Description                  |
| ---------------- | -------------------------------------------------- | ---------------------------- |
| `FACEIT_API_KEY` | _(required)_                                       | Your FACEIT Data API v4 key  |
| `DATABASE_URL`   | `postgresql://didyou:didyou@localhost:5432/didyou` | PostgreSQL connection string |
| `PORT`           | `5000`                                             | Server port                  |

## Architecture

### SSE Streaming

All three features use Server-Sent Events to stream results to the frontend in real time. The server sends `retry: 86400000` on connection start to prevent browser auto-reconnect after the stream completes. Client-side deduplication guards against edge cases.

### API Rate Limiting

The FACEIT API client uses an `asyncio.Semaphore` to limit concurrent requests (default: 5) and retries on 503/429 errors with exponential backoff.

### Caching Strategy

| Data                  | Cache TTL | Rationale                                           |
| --------------------- | --------- | --------------------------------------------------- |
| Match roster & voting | Permanent | Immutable once match is created                     |
| Player lifetime stats | 3 days    | Changes slowly, expensive to fetch                  |
| Match details         | 90 days   | Historical data, cleaned up periodically            |
| Matchroom snapshots   | 7 days    | Lobby data doesn't change after creation            |
| Player scan state     | 6 hours   | Before re-fetching recent history                   |
| Ban status            | Adaptive  | Clean: 7d, active ban: until expiry, permanent: 90d |

### Edge Scoring (Matchroom)

The matchroom analyzer computes an "Edge" score for each map per team:

- **80% HLTV 2.0 rating delta** — match-count-weighted average across all players on that map, with a mild confidence curve (full weight at ~30 avg matches)
- **20% win rate delta** — match-count-weighted, with a heavier confidence curve (full weight at ~100 avg matches) since WR is noisier at low sample sizes

Maps are sorted from best-for-Team-A (top) to best-for-Team-B (bottom). The top and bottom maps show PICK/BAN tags from each team's perspective.

### Search Flow

1. Resolve nicknames → UUIDs via FACEIT API
2. Fetch full match history for both players (paginated, cached per player)
3. Check all matches in parallel batches of 10 for shared participation
4. Stream results to the frontend via SSE as they're found

### Matchroom Flow

1. Fetch match data (cached permanently)
2. Fetch all 10 players' lifetime stats concurrently (cached 3 days)
3. Compute per-team Edge scores for each map
4. Frontend renders unified map list with dual-perspective Edge scores and expandable player breakdowns

### Ban Scanner Flow

1. Resolve player nickname → UUID
2. Fetch match history page (paginated via timestamps)
3. Collect unique opponents, bulk-check ban status (cached with adaptive TTL)
4. Stream banned players with their matches as they're found
5. Supports "load more" pagination for deep history scans

## FACEIT API Notes

- Base URL: `https://open.faceit.com/data/v4`
- Auth: Bearer token
- Player identity: always UUID-first (nicknames are recycled on FACEIT)
- History pagination: max offset 1000, use timestamp pagination (`to` param) for full history
- Player stats: `/players/{id}/stats/cs2` returns lifetime per-map stats with `type:"Map"`, `mode:"5v5"` segments

## Development

```bash
# Lint
uv run ruff check src/
uv run ruff format src/

# Test
uv run pytest

# Build Docker image
docker build -t didyoufaceit .
```

## License

MIT
