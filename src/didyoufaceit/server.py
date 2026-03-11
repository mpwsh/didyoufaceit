"""FastAPI web server for faceit-finder."""

from __future__ import annotations

import json
import logging
import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .api import FaceitClient
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
    return JSONResponse(content={}, status_code=204)


@app.get("/api/search")
async def search(
    you: str = Query("", alias="you"),
    opponent: str = Query("", alias="opponent"),
):
    you = you.strip()
    opponent = opponent.strip()
    if not you or not opponent:
        return JSONResponse({"error": "Both nicknames are required"}, status_code=400)

    api_key = _get_api_key()
    log.info("SSE search: you=%r opponent=%r", you, opponent)

    async def generate():
        try:
            async with FaceitClient(api_key) as client:
                async for event in stream_search(client, you, opponent):
                    yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Stream failed:\n%s", tb)
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


@app.get("/api/matchroom")
async def matchroom(id: str = Query("", alias="id")):
    match_id = id.strip()
    if "room/" in match_id:
        match_id = match_id.split("room/")[-1].split("?")[0].strip()
    if not match_id:
        return JSONResponse({"error": "match id required"}, status_code=400)

    api_key = _get_api_key()
    log.info("Matchroom SSE: id=%r", match_id)

    async def generate():
        try:
            async with FaceitClient(api_key) as client:
                async for event in stream_matchroom(client, match_id):
                    yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Matchroom stream failed:\n%s", tb)
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


@app.get("/{faceit_path:path}")
async def faceit_url_redirect(faceit_path: str):
    """Catch FACEIT URLs pasted directly into the browser bar."""
    if "room/" in faceit_path:
        match_id = faceit_path.split("room/")[-1].split("?")[0].split("/")[0].strip()
        if match_id:
            return RedirectResponse(f"/?room={match_id}")
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
