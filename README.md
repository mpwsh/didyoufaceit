# faceit-finder

Find FACEIT matches where you played against (or alongside) a specific player.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Usage

```bash
# Set your API key once (get it free at https://developers.faceit.com)
export FACEIT_API_KEY="your-key-here"

# Find all matches between you and an opponent
uv run faceit-finder --you yourNickname --opponent theirNickname

# Specify a different game (default: cs2)
uv run faceit-finder --you yourNickname --opponent theirNickname --game csgo

# Scan more history (default 50 pages x 100 = 5000 matches)
uv run faceit-finder --you yourNickname --opponent theirNickname --max-pages 100

# Pass the API key inline
uv run faceit-finder --api-key YOUR_KEY --you yourNickname --opponent theirNickname
```

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--api-key` | `$FACEIT_API_KEY` | FACEIT API key |
| `--you` | required | Your FACEIT nickname |
| `--opponent` | required | Opponent's FACEIT nickname |
| `--game` | `cs2` | Game slug (cs2, csgo, valorant, ...) |
| `--max-pages` | `50` | Pages of history to scan (100 matches/page) |
| `--show-teams` | on | Show full team rosters per match |
