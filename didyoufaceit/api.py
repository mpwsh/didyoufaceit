"""FACEIT API v4 async client."""

from __future__ import annotations
import logging
import httpx

BASE_URL = "https://open.faceit.com/data/v4"
log = logging.getLogger(__name__)


class FaceitAPIError(Exception):
    def __init__(self, status_code: int, url: str, body: str):
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"FACEIT API {status_code} on {url}: {body}")


class FaceitClient:
    def __init__(self, api_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20,
        )

    async def _get(self, path: str, **params) -> dict:
        log.debug("GET %s params=%s", path, params)
        r = await self._client.get(path, params={k: v for k, v in params.items() if v is not None})
        if not r.is_success:
            body = r.text[:500]
            log.error("FACEIT API error %d on %s — %s", r.status_code, path, body)
            raise FaceitAPIError(r.status_code, path, body)
        return r.json()

    async def get_player(self, nickname: str) -> dict:
        log.info("Resolving player: %r", nickname)
        return await self._get("/players", nickname=nickname)

    async def get_player_by_id(self, player_id: str) -> dict:
        return await self._get(f"/players/{player_id}")

    async def get_match_history(self, player_id: str, game: str = "cs2",
                                offset: int = 0, limit: int = 100,
                                to: int | None = None) -> dict:
        log.debug("History page offset=%d limit=%d to=%s", offset, limit, to)
        return await self._get(
            f"/players/{player_id}/history",
            game=game, offset=offset, limit=limit, to=to,
        )

    async def get_match(self, match_id: str) -> dict:
        return await self._get(f"/matches/{match_id}")

    async def get_match_stats(self, match_id: str) -> dict:
        return await self._get(f"/matches/{match_id}/stats")

    async def get_player_stats(self, player_id: str, game: str = "cs2") -> dict:
        return await self._get(f"/players/{player_id}/stats/{game}")

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FaceitClient":
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()
