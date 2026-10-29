"""Exercise real request accounting with HTTPX mocks and a mocked Telegram preflight."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from droppoint.bot import build_application
from droppoint.config import Settings
from droppoint.limits import TripWork, WorkLimitExceeded, gather_cancel, trip_scope
from droppoint.onemap import LatLng, OneMapClient

A = LatLng(1.3, 103.8)
B = LatLng(1.4, 103.9)
DEPART = datetime(2026, 10, 29, tzinfo=UTC)


async def test_actual_retry_attempts_stop_before_exceeding_trip_allowance():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OneMapClient(token="synthetic", http=http, backoff_base=0)
        work = TripWork(max_attempts=2)
        with pytest.raises(WorkLimitExceeded):
            async with trip_scope(work):
                await client.drive(A, B, DEPART)
        assert len(calls) == work.attempts == 2
        assert not work.cache


async def test_simultaneous_searches_share_one_login_and_common_concurrency():
    logins = 0
    active = 0
    maximum = 0

    async def handler(request):
        nonlocal logins, active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0)
            if request.method == "POST":
                logins += 1
                return httpx.Response(
                    200, json={"access_token": "synthetic", "expiry_timestamp": "2000000000"}
                )
            return httpx.Response(200, json={"results": []})
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OneMapClient(
            email="demo@example.test", password="synthetic", http=http, max_concurrency=2
        )
        async with trip_scope(TripWork()):
            await gather_cancel(*(client.geocode("synthetic-place") for _ in range(5)))
        assert logins == 1
        assert maximum <= 2


async def test_real_route_sibling_cannot_restore_cache_after_failure():
    waiting = asyncio.Event()
    stopped = asyncio.Event()

    async def handler(request):
        if request.url.params["routeType"] == "pt":
            waiting.set()
            try:
                await asyncio.Event().wait()
                return httpx.Response(200, json={"plan": {"itineraries": [{"duration": 600}]}})
            finally:
                stopped.set()
        await waiting.wait()
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OneMapClient(token="synthetic", http=http)
        work = TripWork()
        with pytest.raises(httpx.HTTPStatusError):
            async with trip_scope(work):
                await gather_cancel(client.drive(A, B, DEPART), client.pt(A, B, DEPART))
        assert stopped.is_set()
        await asyncio.sleep(0)
        assert not work.cache and not client._cache


async def test_large_retry_after_is_rejected_without_waiting_or_retrying():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OneMapClient(token="synthetic", http=http)
        with pytest.raises(WorkLimitExceeded):
            await client.drive(A, B, DEPART)
        assert len(calls) == 1


@pytest.mark.parametrize("allow,configured", [(False, False), (False, True), (True, True)])
async def test_startup_preflight_requires_explicit_existing_webhook_replacement(allow, configured):
    app = build_application(
        Settings(telegram_bot_token="synthetic", allow_webhook_reset=allow), object()
    )
    bot = SimpleNamespace(
        get_webhook_info=AsyncMock(
            return_value=SimpleNamespace(url="https://example.test/webhook" if configured else "")
        ),
        delete_webhook=AsyncMock(),
    )
    if configured and not allow:
        with pytest.raises(RuntimeError, match="already has a webhook"):
            await app.post_init(SimpleNamespace(bot=bot))
    else:
        await app.post_init(SimpleNamespace(bot=bot))
    bot.delete_webhook.assert_not_called()
