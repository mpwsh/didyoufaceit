"""FACEIT Data API v4 client: one per process, with a concurrency cap and retries."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time

import httpx

BASE_URL = os.environ.get("FACEIT_BASE_URL", "https://open.faceit.com/data/v4")  # override for local testing
log = logging.getLogger(__name__)

# Max concurrent requests to FACEIT from this process (all users share it)
MAX_CONCURRENT = int(os.environ.get("FACEIT_MAX_CONCURRENT", "8"))
MAX_RETRIES = 4
BACKOFF_BASE = 0.5  # seconds: 0.5, 1, 2, 4 (+ jitter)
BACKOFF_CAP = 10.0
RETRY_STATUSES = {429, 500, 502, 503, 504}
HISTORY_PAGE_MAX = 100  # FACEIT caps `limit` at 100 for history and stats lists

# FACEIT's own site loads rooms from this unofficial, keyless endpoint. The Data API leaves a live
# room's voting data empty until the veto is over; the site has to show the pool during the veto,
# so this is tried for live rooms only, best effort. FACEIT_WEB_FALLBACK=0 turns it off.
WEB_MATCH_URL = os.environ.get("FACEIT_WEB_MATCH_URL", "https://www.faceit.com/api/match/v2/match/{match_id}")
WEB_FALLBACK = os.environ.get("FACEIT_WEB_FALLBACK", "1").lower() not in ("0", "false", "no", "off")
WEB_USER_AGENT = "didyoufaceit (+https://github.com/mpwsh/didyoufaceit)"
WEB_CACHE_SECONDS = 10  # every viewer of a live room polls; one lookup per room per 10 s is enough


class FaceitAPIError(Exception):
    def __init__(self, status_code: int, url: str, body: str):
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"FACEIT API {status_code} on {url}")


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return max(0.0, min(float(value), BACKOFF_CAP)) if value else None
    except ValueError:
        return None


class FaceitClient:
    """Shared by every request in the process (created in the app lifespan)."""

    def __init__(self, api_key: str, max_concurrent: int = MAX_CONCURRENT, transport=None, web_transport=None) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(20, connect=5),
            limits=httpx.Limits(max_connections=max_concurrent * 2, max_keepalive_connections=max_concurrent),
            transport=transport,
        )
        self._sem = asyncio.Semaphore(max_concurrent)
        # Separate client: the Data API key is never sent to the site
        self._web = httpx.AsyncClient(
            headers={"Accept": "application/json", "User-Agent": WEB_USER_AGENT},
            timeout=httpx.Timeout(6, connect=3),
            transport=web_transport,
        )
        self._web_cache: dict[str, tuple[float, dict | None]] = {}

    async def _get(self, path: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(MAX_RETRIES + 1):
            wait: float | None = None
            async with self._sem:  # held only for the request itself, never while sleeping
                try:
                    r = await self._client.get(path, params=params)
                except httpx.TransportError as e:  # includes timeouts
                    if attempt == MAX_RETRIES:
                        raise FaceitAPIError(0, path, f"{type(e).__name__}: {e}") from e
                    log.warning("FACEIT %s on %s, retrying (%d/%d)", type(e).__name__, path, attempt + 1, MAX_RETRIES)
                else:
                    if r.is_success:
                        return r.json()
                    if r.status_code not in RETRY_STATUSES or attempt == MAX_RETRIES:
                        body = r.text[:500]
                        log.error("FACEIT API %d on %s: %s", r.status_code, path, body)
                        raise FaceitAPIError(r.status_code, path, body)
                    wait = _retry_after(r)
                    log.warning("FACEIT API %d on %s, retrying (%d/%d)", r.status_code, path, attempt + 1, MAX_RETRIES)
            if wait is None:
                wait = min(BACKOFF_BASE * 2**attempt, BACKOFF_CAP)
            await asyncio.sleep(wait * (0.8 + 0.4 * random.random()))
        raise FaceitAPIError(0, path, "retries exhausted")  # not reached: the last attempt raises

    async def get_player(self, nickname: str) -> dict:
        return await self._get("/players", nickname=nickname)

    async def get_player_by_id(self, player_id: str) -> dict:
        return await self._get(f"/players/{player_id}")

    async def get_match_history(
        self, player_id: str, game: str = "cs2", offset: int = 0, limit: int = 100, to: int | None = None
    ) -> dict:
        """Items carry rosters (player ids, nicknames, avatars, Steam ids), series score and timestamps.
        `from` defaults to 0 (all time) on FACEIT's side."""
        return await self._get(
            f"/players/{player_id}/history",
            game=game,
            offset=offset,
            limit=min(limit, HISTORY_PAGE_MAX),
            to=to,
        )

    async def get_match(self, match_id: str) -> dict:
        return await self._get(f"/matches/{match_id}")

    async def get_match_stats(self, match_id: str) -> dict:
        """Map, per-map round score and per-player stats for each map of the match."""
        return await self._get(f"/matches/{match_id}/stats")

    async def get_player_stats(self, player_id: str, game: str = "cs2") -> dict:
        return await self._get(f"/players/{player_id}/stats/{game}")

    async def get_player_match_stats(
        self, player_id: str, game: str = "cs2", offset: int = 0, limit: int = 100
    ) -> dict:
        """One row of stats per recent match (map, rounds, kills, deaths, assists, ADR, result)."""
        return await self._get(
            f"/players/{player_id}/games/{game}/stats", offset=offset, limit=min(limit, HISTORY_PAGE_MAX)
        )

    async def get_player_bans(self, player_id: str) -> list[dict]:
        """Ban records (active and expired); [] when FACEIT has none (404)."""
        try:
            data = await self._get(f"/players/{player_id}/bans", offset=0, limit=20)
        except FaceitAPIError as e:
            if e.status_code == 404:
                return []
            raise
        return data.get("items", [])

    async def get_match_web(self, match_id: str) -> dict | None:
        """The match as FACEIT's site loads it (`payload` of the unofficial endpoint). Best effort:
        None when turned off, blocked, changed or slow. One lookup per room per WEB_CACHE_SECONDS."""
        if not WEB_FALLBACK:
            return None
        now = time.monotonic()
        hit = self._web_cache.get(match_id)
        if hit and now - hit[0] < WEB_CACHE_SECONDS:
            return hit[1]
        payload = None
        try:
            async with self._sem:
                r = await self._web.get(WEB_MATCH_URL.format(match_id=match_id))
            if r.is_success:
                data = r.json()
                payload = data.get("payload") if isinstance(data, dict) else None
            else:
                log.info("FACEIT web match lookup %d for %s", r.status_code, match_id)
        except (httpx.HTTPError, ValueError) as e:
            log.info("FACEIT web match lookup failed for %s: %s", match_id, e)
        if len(self._web_cache) > 1000:
            self._web_cache.clear()
        self._web_cache[match_id] = (now, payload if isinstance(payload, dict) else None)
        return self._web_cache[match_id][1]

    async def close(self) -> None:
        await self._client.aclose()
        await self._web.aclose()
