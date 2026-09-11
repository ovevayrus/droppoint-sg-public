"""Offline tests for the OneMap client, using httpx.MockTransport and saved fixtures.

No real network is touched. These cover: the static-token path, the email/password
path with a 401 refresh-and-retry, geocoding a postal code, an empty geocode result,
a drive route, a public-transport route, a no-route response, and a 429 backoff.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from droppoint.onemap import (
    BASE_URL,
    LatLng,
    OneMapAuthError,
    OneMapClient,
    seconds_to_minutes,
)
from support import load_fixture

DEPART = datetime(2026, 6, 28, 9, 0, tzinfo=UTC)
A = LatLng(1.3000, 103.8000)
B = LatLng(1.3500, 103.8500)


def make_client(handler, **kwargs) -> OneMapClient:
    """Build a real OneMapClient whose HTTP layer is a mock transport."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="")
    return OneMapClient(http=http, backoff_base=0.0, **kwargs)


def test_seconds_to_minutes():
    assert seconds_to_minutes(1200) == 20.0
    assert seconds_to_minutes(90) == 1.5


async def test_static_token_is_used_directly():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, json=load_fixture("route_drive.json"))

    client = make_client(handler, token="static-token-xyz")
    minutes = await client.drive(A, B, DEPART)

    assert minutes == 20.0
    assert seen["auth"] == "static-token-xyz"
    assert seen["path"] == "/api/public/routingsvc/route"
    await client.aclose()


async def test_base_url_is_onemap_gov_sg():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["host"] = request.url.host
        return httpx.Response(200, json=load_fixture("route_drive.json"))

    client = make_client(handler, token="t")
    await client.drive(A, B, DEPART)
    assert captured["host"] == "www.onemap.gov.sg"
    assert BASE_URL == "https://www.onemap.gov.sg"
    await client.aclose()


async def test_email_password_login_and_refresh_on_401():
    state = {"logins": 0, "route_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/post/getToken"):
            state["logins"] += 1
            token = f"token-{state['logins']}"
            return httpx.Response(
                200, json={"access_token": token, "expiry_timestamp": "1900000000"}
            )
        # Routing endpoint: first call 401, second (after refresh) succeeds.
        state["route_calls"] += 1
        if state["route_calls"] == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json=load_fixture("route_drive.json"))

    client = make_client(handler, email="a@b.com", password="pw")
    minutes = await client.drive(A, B, DEPART)

    assert minutes == 20.0
    # One initial login, one refresh after the 401.
    assert state["logins"] == 2
    assert state["route_calls"] == 2
    await client.aclose()


async def test_static_token_expired_raises_clear_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "expired"})

    client = make_client(handler, token="dead-token")
    with pytest.raises(OneMapAuthError) as exc:
        await client.drive(A, B, DEPART)
    assert "ONEMAP_TOKEN" in str(exc.value)
    await client.aclose()


async def test_no_credentials_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load_fixture("route_drive.json"))

    client = make_client(handler)  # no token, no email/password
    with pytest.raises(OneMapAuthError):
        await client.drive(A, B, DEPART)
    await client.aclose()


async def test_geocode_postal_code():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/common/elastic/search"
        assert request.url.params["searchVal"] == "307470"
        assert request.headers["Authorization"] == "test-token"
        return httpx.Response(200, json=load_fixture("search_postal.json"))

    client = make_client(handler, token="test-token")
    result = await client.geocode("307470")
    assert result is not None
    assert result.point == LatLng(1.320359, 103.843818)
    assert "NOVENA" in result.label
    await client.aclose()


async def test_geocode_empty_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load_fixture("search_empty.json"))

    client = make_client(handler, token="test-token")
    assert await client.geocode("zzzz nowhere") is None
    await client.aclose()


async def test_geocode_auth_error_is_generic():
    private_detail = "rejected account person@example.test"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": private_detail})

    client = make_client(handler, token="test-token")
    with pytest.raises(OneMapAuthError) as exc:
        await client.geocode("111 Example Street")
    assert str(exc.value) == "OneMap rejected the search request."
    assert private_detail not in str(exc.value)
    await client.aclose()


async def test_clear_cache_discards_route_coordinates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load_fixture("route_drive.json"))

    client = make_client(handler, token="test-token")
    await client.drive(A, B, DEPART)
    assert client._cache
    client.clear_cache()
    assert client._cache == {}
    await client.aclose()


async def test_drive_route_minutes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["routeType"] == "drive"
        return httpx.Response(200, json=load_fixture("route_drive.json"))

    client = make_client(handler, token="t")
    assert await client.drive(A, B, DEPART) == 20.0
    await client.aclose()


async def test_pt_route_minutes_and_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=load_fixture("route_pt.json"))

    client = make_client(handler, token="t")
    minutes = await client.pt(A, B, DEPART)

    assert minutes == 90.0  # min itinerary duration 5400s
    assert captured["routeType"] == "pt"
    assert captured["date"] == "06-28-2026"  # OneMap requires MM-DD-YYYY
    assert captured["time"] == "09:00:00"
    assert captured["mode"] == "TRANSIT"
    assert "maxWalkDistance" in captured
    assert "numItineraries" in captured
    await client.aclose()


async def test_route_with_no_result_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load_fixture("route_drive_empty.json"))

    client = make_client(handler, token="t")
    assert await client.drive(A, B, DEPART) is None
    await client.aclose()


async def test_pt_with_no_itineraries_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load_fixture("route_pt_empty.json"))

    client = make_client(handler, token="t")
    assert await client.pt(A, B, DEPART) is None
    await client.aclose()


async def test_route_404_means_no_route_not_an_error():
    """OneMap answers 404 for unroutable pairs (e.g. start == end); treat as None."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        return httpx.Response(404, json={"error": "No route found"})

    client = make_client(handler, token="t")
    assert await client.drive(A, A, DEPART) is None
    assert await client.pt(A, A, DEPART) is None
    # The None result is cached like any other.
    assert await client.drive(A, A, DEPART) is None
    assert state["calls"] == 2
    await client.aclose()


async def test_429_backoff_then_success():
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] < 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=load_fixture("route_drive.json"))

    client = make_client(handler, token="t")  # backoff_base=0.0 keeps the test fast
    assert await client.drive(A, B, DEPART) == 20.0
    assert state["calls"] == 3
    await client.aclose()


async def test_transient_network_error_is_retried():
    """A single timeout among many routing calls must not fail the whole plan."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(200, json=load_fixture("route_drive.json"))

    client = make_client(handler, token="t")
    assert await client.drive(A, B, DEPART) == 20.0
    assert state["calls"] == 2
    await client.aclose()


async def test_persistent_network_error_still_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    client = make_client(handler, token="t", max_retries=2)
    with pytest.raises(httpx.ConnectError):
        await client.drive(A, B, DEPART)
    await client.aclose()


async def test_response_is_cached():
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        return httpx.Response(200, json=load_fixture("route_drive.json"))

    client = make_client(handler, token="t")
    await client.drive(A, B, DEPART)
    await client.drive(A, B, DEPART)  # same key -> served from cache
    assert state["calls"] == 1
    await client.aclose()
