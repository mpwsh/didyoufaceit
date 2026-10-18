"""FastAPI web server: three SSE endpoints and the static pages."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import anyio
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .api import FaceitAPIError, FaceitClient
from .banscan import stream_ban_scan
from .cache import close_db, init_db
from .matchroom import match_pool, stream_matchroom
from .search import stream_search

load_dotenv()  # .env in the working directory, if any; real environment variables win

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per FACEIT call is noise

PING_SECONDS = 15  # SSE comment while a step is busy, so proxies don't drop an idle stream
MATCH_ID_RE = re.compile(r"1-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
NICKNAME_MAX = 32
_static_dir = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    key = os.environ.get("FACEIT_API_KEY", "")
    # One client per process: its connection pool and concurrency cap are shared by every request
    app.state.faceit = FaceitClient(key) if key else None
    if not key:
        log.error("FACEIT_API_KEY is not set; API endpoints will return 503")
    yield
    if app.state.faceit:
        await app.state.faceit.close()
    await close_db()


app = FastAPI(title="didyou.gg", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ── SSE ───────────────────────────────────────────────────────────────────────


def _error_event(exc: BaseException) -> dict:
    """A browser-safe error: a status the page can act on, no response bodies or tracebacks."""
    if isinstance(exc, FaceitAPIError):
        status = exc.status_code or 503
        msg = {
            404: "Not found on FACEIT",
            401: "FACEIT rejected our API key",
            403: "FACEIT rejected our API key",
            429: "FACEIT is rate limiting us. Try again in a minute.",
        }.get(status, "FACEIT is having trouble. Try again in a moment.")
        return {"type": "error", "status": status, "msg": msg}
    return {"type": "error", "status": 500, "msg": "Something went wrong"}


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _sse_response(events: AsyncIterator[dict], label: str) -> StreamingResponse:
    """Stream events as SSE, with a ping comment every PING_SECONDS of silence.

    On client disconnect Starlette cancels the response; the step in flight is cancelled and the
    event generator closed, so no further FACEIT calls are made. (If the cancel lands while a
    chunk is being sent rather than awaited, cleanup waits for garbage collection; at most the
    step already running finishes.)
    """

    async def generate():
        # Disable EventSource auto-reconnect: the page closes the stream itself after "done"
        yield "retry: 86400000\n\n"
        pending: asyncio.Task | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(anext(events))
                done, _ = await asyncio.wait({pending}, timeout=PING_SECONDS)
                if not done:
                    yield ": ping\n\n"
                    continue
                task, pending = pending, None
                try:
                    event = task.result()
                except StopAsyncIteration:
                    return
                yield _sse(event)
        except asyncio.CancelledError:
            log.info("Client went away: %s", label)
            raise
        except Exception as exc:
            log.exception("%s failed", label)
            yield _sse(_error_event(exc))
        finally:
            with anyio.CancelScope(shield=True):
                if pending is not None and not pending.done():
                    pending.cancel()
                    try:
                        await pending
                    except BaseException:  # noqa: BLE001 - only waiting for it to stop
                        pass
                await events.aclose()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _client(request: Request) -> FaceitClient | None:
    return getattr(request.app.state, "faceit", None)


def _bad_request(msg: str, status: int = 400) -> StreamingResponse:
    """An SSE stream with a single error event.

    EventSource can't read the body of a non-200 response, so a plain 400 would only show up
    as "Connection lost" on the page.
    """

    async def one():
        yield {"type": "error", "status": status, "msg": msg}

    return _sse_response(one(), "rejected")


def _match_id(raw: str) -> str | None:
    """A FACEIT match id from an id or a room URL; None if there isn't one."""
    found = MATCH_ID_RE.search(raw.strip().lower())
    return found.group(0) if found else None


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/api/search")
async def search(request: Request, you: str = Query(""), opponent: str = Query("")):
    you, opponent = you.strip(), opponent.strip()
    if not you or not opponent:
        return _bad_request("Both nicknames are required")
    if len(you) > NICKNAME_MAX or len(opponent) > NICKNAME_MAX:
        return _bad_request("Nickname too long")
    if you.lower() == opponent.lower():
        return _bad_request("Pick two different players")
    client = _client(request)
    if client is None:
        return _bad_request("Server is missing its FACEIT API key", 503)
    log.info("SSE search: you=%r opponent=%r", you, opponent)
    return _sse_response(stream_search(client, you, opponent), f"search({you} vs {opponent})")


@app.get("/api/matchroom")
async def matchroom(request: Request, id: str = Query("")):
    match_id = _match_id(id)
    if not match_id:
        return _bad_request("Paste a FACEIT match room link or id (1-xxxxxxxx-…)")
    client = _client(request)
    if client is None:
        return _bad_request("Server is missing its FACEIT API key", 503)
    log.info("Matchroom SSE: id=%s", match_id)
    return _sse_response(stream_matchroom(client, match_id), f"matchroom({match_id})")


@app.get("/api/matchroom/pool")
async def matchroom_pool(request: Request, id: str = Query("")):
    """The room's map pool and veto state as JSON; the page polls it while a live room has no pool yet."""
    match_id = _match_id(id)
    if not match_id:
        return JSONResponse({"error": "Not a FACEIT match id"}, status_code=400)
    client = _client(request)
    if client is None:
        return JSONResponse({"error": "Server is missing its FACEIT API key"}, status_code=503)
    try:
        return JSONResponse(await match_pool(client, match_id))
    except FaceitAPIError as e:
        status = 404 if e.status_code == 404 else 502
        return JSONResponse(
            {"error": "Match not found" if status == 404 else "FACEIT is unavailable"}, status_code=status
        )


@app.get("/api/banscan")
async def ban_scan(
    request: Request,
    player: str = Query(""),
    before: int | None = Query(None, ge=0),
    limit: int = Query(100, ge=1, le=100),
):
    """Scan a player's match history for banned opponents.

    Paginated by timestamp: the first call scans the latest matches; pass the response's
    `next_before_ts` as `before` to continue further back.
    """
    player = player.strip()
    if not player:
        return _bad_request("player nickname required")
    if len(player) > NICKNAME_MAX:
        return _bad_request("Nickname too long")
    client = _client(request)
    if client is None:
        return _bad_request("Server is missing its FACEIT API key", 503)
    log.info("Ban scan SSE: player=%r before=%s limit=%d", player, before, limit)
    return _sse_response(stream_ban_scan(client, player, limit=limit, before_ts=before), f"banscan({player})")


# ── Pages ─────────────────────────────────────────────────────────────────────


@app.get("/")
async def index():
    return FileResponse(str(_static_dir / "index.html"))


@app.get("/vs")
async def vs_page():
    return FileResponse(str(_static_dir / "vs.html"))


@app.get("/matchroom")
async def matchroom_page():
    return FileResponse(str(_static_dir / "matchroom.html"))


@app.get("/banscan")
async def banscan_page():
    return FileResponse(str(_static_dir / "banscan.html"))


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/{faceit_path:path}")
async def faceit_url_redirect(faceit_path: str):
    """A FACEIT room URL pasted after our domain (didyou.gg/en/cs2/room/1-…) opens the matchroom."""
    match_id = _match_id(faceit_path) if "room/" in faceit_path else None
    if match_id:
        return RedirectResponse(f"/matchroom?id={quote(match_id)}")
    return JSONResponse({"error": "not found"}, status_code=404)


def run():
    """Dev server entrypoint (`uv run didyoufaceit-web`)."""
    import uvicorn

    port = int(os.environ.get("PORT", 5000))
    log.info("Starting didyoufaceit on http://0.0.0.0:%d", port)
    uvicorn.run("didyoufaceit.server:app", host="0.0.0.0", port=port, reload=True, reload_dirs=["src"])
