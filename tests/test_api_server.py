"""FACEIT client retries, the SSE wrapper and request validation."""

import asyncio

import httpx
import pytest

from didyoufaceit import api, server
from didyoufaceit.api import FaceitAPIError, FaceitClient


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)
    return waits


def client_for(handler) -> FaceitClient:
    return FaceitClient("k", transport=httpx.MockTransport(handler))


async def test_negative_retry_after_is_ignored(no_sleep):
    responses = iter([httpx.Response(429, headers={"Retry-After": "-5"}), httpx.Response(200, json={})])
    await client_for(lambda req: next(responses))._get("/x")
    assert no_sleep == [0.0]


async def test_retries_rate_limit_and_honours_retry_after(no_sleep):
    responses = iter([httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json={"ok": 1})])
    c = client_for(lambda req: next(responses))
    assert await c._get("/x") == {"ok": 1}
    assert 2.4 <= no_sleep[0] <= 3.6  # Retry-After with ±20% jitter


async def test_retries_timeouts():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"ok": 1})

    assert await client_for(handler)._get("/x") == {"ok": 1}
    assert calls["n"] == 3


async def test_gives_up_with_status():
    c = client_for(lambda req: httpx.Response(503, text="busy"))
    with pytest.raises(FaceitAPIError) as e:
        await c._get("/x")
    assert e.value.status_code == 503


async def test_404_is_not_retried_and_bans_404_means_none():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(404)

    c = client_for(handler)
    with pytest.raises(FaceitAPIError):
        await c.get_player("nobody")
    assert calls["n"] == 1
    assert await c.get_player_bans("p") == []


async def test_history_limit_is_capped_at_100():
    seen = {}

    def handler(req):
        seen.update(req.url.params)
        return httpx.Response(200, json={"items": []})

    await client_for(handler).get_match_history("p", limit=500)
    assert seen["limit"] == "100"


# ── SSE wrapper ───────────────────────────────────────────────────────────────


async def body_of(response) -> str:
    return "".join([chunk async for chunk in response.body_iterator])


async def test_sse_pings_while_a_step_is_slow(monkeypatch):
    monkeypatch.setattr(server, "PING_SECONDS", 0.05)
    closed = []

    async def events():
        try:
            await asyncio.wait_for(asyncio.Event().wait(), 0.2)  # real sleep: asyncio.sleep is patched
        except TimeoutError:
            pass
        yield {"type": "done"}
        closed.append(True)

    text = await body_of(server._sse_response(events(), "t"))
    assert text.startswith("retry: 86400000")
    assert ": ping" in text
    assert text.rstrip().endswith('data: {"type": "done"}')


async def test_sse_maps_errors_without_leaking_details():
    async def events():
        yield {"type": "status", "msg": "x"}
        raise FaceitAPIError(429, "/players/abc/history", "secret body")

    text = await body_of(server._sse_response(events(), "t"))
    assert '"status": 429' in text and "secret body" not in text and "/players" not in text

    async def boom():
        raise RuntimeError("db password is hunter2")
        yield {}

    text = await body_of(server._sse_response(boom(), "t"))
    assert '"status": 500' in text and "hunter2" not in text


async def test_sse_closes_the_generator_when_the_client_leaves():
    state = {"closed": False}

    async def events():
        try:
            yield {"type": "status"}
            await asyncio.Event().wait()  # a step that never finishes, like a slow FACEIT call
            yield {"type": "never"}
        finally:
            state["closed"] = True

    response = server._sse_response(events(), "t")
    it = response.body_iterator
    assert (await anext(it)).startswith("retry")
    assert "status" in await anext(it)
    task = asyncio.ensure_future(anext(it))
    await asyncio.sleep(0)
    task.cancel()  # what Starlette does on disconnect
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state["closed"]


# ── validation (no FACEIT or database needed) ─────────────────────────────────


@pytest.fixture
def http():
    server.app.state.faceit = object()  # endpoints only check that a client exists before validating
    transport = httpx.ASGITransport(app=server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.parametrize(
    "url",
    [
        "/api/matchroom?id=../../players/abc",
        "/api/matchroom?id=room/",
        "/api/search?you=abc&opponent=ABC",
        "/api/search?you=abc",
        "/api/banscan?player=" + "x" * 40,
    ],
)
async def test_bad_input_is_an_sse_error_the_page_can_show(http, url):
    # EventSource can't read a 400 body, so rejections arrive as one error event
    r = await http.get(url)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert '"type": "error"' in r.text and '"status": 400' in r.text


async def test_out_of_range_limit_is_rejected(http):
    assert (await http.get("/api/banscan?player=x&limit=500")).status_code == 422


def test_match_id_from_urls():
    mid = "1-933a200e-5a1a-43a9-b9d5-d1fe703acaca"
    assert server._match_id(f"https://www.faceit.com/en/cs2/room/{mid}/scoreboard") == mid
    assert server._match_id(mid.upper()) == mid
    assert server._match_id("1-933a200e") is None


async def test_room_url_redirect(http):
    mid = "1-933a200e-5a1a-43a9-b9d5-d1fe703acaca"
    r = await http.get(f"/en/cs2/room/{mid}/scoreboard")
    assert r.status_code == 307 and r.headers["location"] == f"/matchroom?id={mid}"
    assert (await http.get("/en/cs2/room/whatever")).status_code == 404


async def test_pool_endpoint_rejects_bad_ids(http):
    r = await http.get("/api/matchroom/pool?id=../../players/abc")
    assert r.status_code == 400


async def test_pool_endpoint_returns_the_rooms_pool(http, monkeypatch):
    mid = "1-933a200e-5a1a-43a9-b9d5-d1fe703acaca"
    seen = []

    async def fake_pool(client, match_id):
        seen.append(match_id)
        return {"map_pool": ["de_nuke"], "map_pool_source": "match"}

    monkeypatch.setattr(server, "match_pool", fake_pool)
    r = await http.get(f"/api/matchroom/pool?id=https://www.faceit.com/en/cs2/room/{mid}")
    assert r.status_code == 200 and r.json()["map_pool"] == ["de_nuke"]
    assert seen == [mid]


# ── FACEIT site fallback (live rooms) ─────────────────────────────────────────

VOTING = {"map": {"entities": [{"game_map_id": "de_vertigo", "name": "Vertigo"}]}}


def web_client(handler) -> FaceitClient:
    return FaceitClient("secret-key", web_transport=httpx.MockTransport(handler))


async def test_web_match_returns_payload_without_the_api_key():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"code": "OPERATION-OK", "payload": {"voting": VOTING}})

    c = web_client(handler)
    assert (await c.get_match_web("1-abc"))["voting"] == VOTING
    assert seen[0].url.host == "www.faceit.com" and "/match/v2/match/1-abc" in seen[0].url.path
    assert "authorization" not in seen[0].headers


async def test_web_match_is_cached_per_room():
    calls = []
    c = web_client(lambda req: calls.append(1) or httpx.Response(200, json={"payload": {"voting": VOTING}}))
    await c.get_match_web("1-abc")
    await c.get_match_web("1-abc")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, text="<html>blocked</html>"),
        httpx.Response(200, text="<html>challenge</html>"),
        httpx.Response(200, json=["not", "a", "dict"]),
        httpx.Response(200, json={"payload": "nope"}),
    ],
)
async def test_web_match_failures_are_none(response):
    assert await web_client(lambda req: response).get_match_web("1-abc") is None


async def test_web_match_timeout_is_none():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    assert await web_client(handler).get_match_web("1-abc") is None


async def test_web_match_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(api, "WEB_FALLBACK", False)
    c = web_client(lambda req: pytest.fail("should not be called"))
    assert await c.get_match_web("1-abc") is None
