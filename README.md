# didyoufaceit

CS2 FACEIT match history search and matchroom analyzer.

Two features:

- **Player vs Player** — find every match two players have shared (as enemies or teammates), with W/L records, filters, and best lineup analysis
- **Matchroom Analyzer** — paste a FACEIT room URL, get a per-player map breakdown with win rate contributions to help decide which map to pick or ban during veto

## Stack

- **Backend:** Python 3.12+, FastAPI, asyncpg, httpx (async)
- **Frontend:** Alpine.js v3, Tailwind CSS v4 (single page app)
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

## Project Structure

```
.
├── Dockerfile
├── docker-compose.yml
├── gunicorn_conf.py
├── pyproject.toml
└── src/didyoufaceit/
    ├── api.py            # Async FACEIT API client (rate limited, retry)
    ├── cache.py           # PostgreSQL cache layer (asyncpg connection pool)
    ├── search.py          # Player vs player search (batched parallel fetches)
    ├── matchroom.py       # Matchroom analyzer (concurrent player stat fetching)
    ├── server.py          # FastAPI app with SSE endpoints
    ├── cli.py             # CLI entrypoint (rich tables)
    └── static/
        ├── index.html     # Single page app
        └── app.js         # Alpine.js app logic
```

## Architecture

### API Rate Limiting

The FACEIT API client uses an `asyncio.Semaphore` to limit concurrent requests (default: 5) and retries on 503/429 errors with exponential backoff. This prevents hitting FACEIT's rate limits while still fetching data in parallel.

### Caching Strategy

| Data                  | Cache TTL | Rationale                                    |
| --------------------- | --------- | -------------------------------------------- |
| Match roster & voting | Permanent | Immutable once match is created              |
| Player lifetime stats | 3 days    | Changes slowly, expensive to fetch           |
| Match details         | Permanent | Historical data doesn't change               |
| Player scan state     | Permanent | Tracks which players have been fully indexed |

On a first search, the app fetches data from FACEIT and caches it. Subsequent searches for the same players or matchrooms are near-instant with zero API calls.

### Search Flow

1. Resolve nicknames → UUIDs via FACEIT API
2. Fetch full match history for both players (paginated, cached per player)
3. Check all matches in parallel batches of 10 for shared participation
4. Stream results to the frontend via SSE as they're found

### Matchroom Flow

1. Fetch match data (cached permanently)
2. Fetch all 10 players' lifetime stats concurrently (cached 3 days)
3. Compute per-team weighted win rates for each map
4. Frontend renders MAP ADVANTAGE table with pick/ban recommendations

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
