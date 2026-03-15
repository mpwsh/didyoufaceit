"""FastAPI web server for faceit-finder."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .api import FaceitClient
from .banscan import stream_ban_scan
from .cache import close_db, init_db
from .matchroom import stream_matchroom
from .search import stream_search

# Load .env from project root if present
_env_path = Path(__file__).parent.parent.parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _get_api_key() -> str:
    key = os.environ.get("FACEIT_API_KEY", "")
    if not key:
        raise ValueError("FACEIT_API_KEY not set")
    return key


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    log.info("Database initialized")
    yield
    await close_db()
    log.info("Database pool closed")


app = FastAPI(title="didyou.gg", lifespan=lifespan)

# Static files (index.html, app.js)
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(_static_dir / "index.html"))


@app.get("/favicon.ico")
async def favicon():
    from starlette.responses import Response

    return Response(status_code=204)


# ── SSE helper ────────────────────────────────────────────────────────────────


def _sse_response(request: Request, stream_coro, label: str = "stream"):
    """Wrap an async generator in a disconnect-aware SSE StreamingResponse.

    Checks request.is_disconnected() before yielding each event.
    When the client closes the tab, we stop consuming the generator
    which means no further DB queries or API calls are kicked off.
    """

    async def generate():
        try:
            async for event in stream_coro:
                # Check if client is still connected before sending
                if await request.is_disconnected():
                    log.info("Client disconnected, stopping %s", label)
                    return
                yield f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            log.info("Stream cancelled: %s", label)
            return
        except Exception as exc:
            tb = traceback.format_exc()
            log.error("%s failed:\n%s", label, tb)
            yield f"data: {json.dumps({'type': 'error', 'msg': str(exc)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/api/search")
async def search(
    request: Request,
    you: str = Query("", alias="you"),
    opponent: str = Query("", alias="opponent"),
):
    you = you.strip()
    opponent = opponent.strip()
    if not you or not opponent:
        return JSONResponse({"error": "Both nicknames are required"}, status_code=400)

    api_key = _get_api_key()
    log.info("SSE search: you=%r opponent=%r", you, opponent)

    async def _stream():
        async with FaceitClient(api_key) as client:
            async for event in stream_search(client, you, opponent):
                yield event

    return _sse_response(request, _stream(), label=f"search({you} vs {opponent})")


@app.get("/api/matchroom")
async def matchroom(
    request: Request,
    id: str = Query("", alias="id"),
):
    match_id = id.strip()
    if "room/" in match_id:
        match_id = match_id.split("room/")[-1].split("?")[0].strip()
    if not match_id:
        return JSONResponse({"error": "match id required"}, status_code=400)

    api_key = _get_api_key()
    log.info("Matchroom SSE: id=%r", match_id)

    async def _stream():
        async with FaceitClient(api_key) as client:
            async for event in stream_matchroom(client, match_id):
                yield event

    return _sse_response(request, _stream(), label=f"matchroom({match_id})")


@app.get("/api/banscan")
async def ban_scan(
    request: Request,
    player: str = Query("", alias="player"),
    before: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """Scan a player's match history for banned opponents.

    Paginated via timestamps: first call with no `before` param scans most recent matches.
    Response includes `next_before_ts` — pass it as `before` to load the next page.
    No offset limit since we use FACEIT's `to` timestamp parameter.
    """
    player = player.strip()
    if not player:
        return JSONResponse({"error": "player nickname required"}, status_code=400)

    api_key = _get_api_key()
    log.info("Ban scan SSE: player=%r before=%s limit=%d", player, before, limit)

    stop_event = asyncio.Event()

    async def _stream():
        async with FaceitClient(api_key) as client:
            async for event in stream_ban_scan(client, player, limit=limit, before_ts=before, stop_event=stop_event):
                yield event

    async def _generate_with_cancel():
        try:
            async for event in _stream():
                if await request.is_disconnected():
                    log.info("Client disconnected, cancelling ban scan for %s", player)
                    stop_event.set()
                    return
                yield f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            stop_event.set()
            log.info("Ban scan cancelled: %s", player)
            return
        except Exception as exc:
            stop_event.set()
            tb = traceback.format_exc()
            log.error("Ban scan failed:\n%s", tb)
            yield f"data: {json.dumps({'type': 'error', 'msg': str(exc)})}\n\n"

    return StreamingResponse(
        _generate_with_cancel(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/vs")
async def vs_page():
    return FileResponse(str(_static_dir / "vs.html"))


@app.get("/matchroom")
async def matchroom_page():
    return FileResponse(str(_static_dir / "matchroom.html"))


@app.get("/banscan")
async def banscan_page():
    return FileResponse(str(_static_dir / "banscan.html"))


@app.get("/{faceit_path:path}")
async def faceit_url_redirect(faceit_path: str):
    """Catch FACEIT URLs pasted directly into the browser bar."""
    if "room/" in faceit_path:
        match_id = faceit_path.split("room/")[-1].split("?")[0].split("/")[0].strip()
        if match_id:
            return RedirectResponse(f"/matchroom?id={match_id}")
    return JSONResponse({"error": "not found"}, status_code=404)


def run():
    """Dev server entrypoint."""
    import uvicorn

    port = int(os.environ.get("PORT", 5000))
    log.info("Starting didyoufaceit on http://0.0.0.0:%d", port)
    uvicorn.run(
        "didyoufaceit.server:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        reload_dirs=["src"],
        reload_excludes=["*.db", "*.db-wal", "*.db-shm", "*.pyc", "__pycache__"],
    )
