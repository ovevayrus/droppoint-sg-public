"""OneMap client.

A single async wrapper around the OneMap (Singapore Land Authority) HTTP API. The
rest of the code never touches HTTP directly, and the client is swappable for a mock
in tests (anything matching :class:`RouteClient`).

Base URL is ``https://www.onemap.gov.sg`` - the old ``developers.onemap.sg`` base is
deprecated and is not used here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple, Protocol

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://www.onemap.gov.sg"
AUTH_PATH = "/api/auth/post/getToken"
SEARCH_PATH = "/api/common/elastic/search"
ROUTE_PATH = "/api/public/routingsvc/route"

# Public-transport routing wants the date and time split out. OneMap requires the date
# as MM-DD-YYYY (confirmed against the live API; it rejects YYYY-MM-DD with a 400).
PT_DATE_FMT = "%m-%d-%Y"
PT_TIME_FMT = "%H:%M:%S"

# Refresh a fetched token a little before it actually expires.
TOKEN_REFRESH_MARGIN = timedelta(minutes=10)


def seconds_to_minutes(seconds: float) -> float:
    """The single place seconds are converted to minutes."""
    return seconds / 60.0


class LatLng(NamedTuple):
    """A latitude/longitude pair."""

    lat: float
    lng: float

    @property
    def as_param(self) -> str:
        """Format as the ``lat,lng`` string OneMap expects."""
        return f"{self.lat},{self.lng}"


@dataclass(frozen=True)
class GeocodeResult:
    """A resolved location from the search endpoint."""

    label: str
    point: LatLng
    address: str | None = None


class OneMapError(Exception):
    """Base class for OneMap client errors."""


class OneMapAuthError(OneMapError):
    """Authentication failed or no usable credentials were supplied."""


class RouteClient(Protocol):
    """The minimal interface the optimiser depends on. Easy to mock."""

    async def drive(self, start: LatLng, end: LatLng, depart: datetime) -> float | None: ...

    async def pt(self, start: LatLng, end: LatLng, depart: datetime) -> float | None: ...


def _round_depart(depart: datetime) -> str:
    """Round a departure time down to 5 minutes, for stable cache keys."""
    minute = (depart.minute // 5) * 5
    rounded = depart.replace(minute=minute, second=0, microsecond=0)
    return rounded.isoformat()


class OneMapClient:
    """Async OneMap client: auth, geocoding, and drive/public-transport routing."""

    def __init__(
        self,
        *,
        token: str | None = None,
        email: str | None = None,
        password: str | None = None,
        http: httpx.AsyncClient | None = None,
        max_concurrency: int = 5,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        pt_max_walk_distance_m: int = 1000,
        pt_num_itineraries: int = 3,
        timeout: float = 30.0,
    ) -> None:
        self._static_token = token
        self._email = email
        self._password = password
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http is None
        self._sem = asyncio.Semaphore(max_concurrency)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._pt_max_walk_distance_m = pt_max_walk_distance_m
        self._pt_num_itineraries = pt_num_itineraries

        # Fetched (email/password) token state.
        self._token: str | None = None
        self._token_expiry: datetime | None = None

        # Response cache keyed by (route_type, start, end, rounded_depart).
        self._cache: dict[tuple[str, str, str, str], float | None] = {}

    @classmethod
    def from_settings(cls, settings) -> OneMapClient:
        """Build a client from a :class:`droppoint.config.Settings`."""
        return cls(
            token=settings.onemap_token_value,
            email=settings.onemap_email_value,
            password=settings.onemap_password_value,
            pt_max_walk_distance_m=settings.pt_max_walk_distance_m,
            pt_num_itineraries=settings.pt_num_itineraries,
        )

    async def __aenter__(self) -> OneMapClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def clear_cache(self) -> None:
        """Discard route coordinates retained by the in-memory response cache."""
        self._cache.clear()

    # --- Authentication ---------------------------------------------------

    def _can_refresh(self) -> bool:
        return bool(self._email and self._password)

    async def _auth_token(self) -> str:
        """Return a usable token, preferring the self-refreshing email/password flow."""
        if self._can_refresh():
            if (
                self._token
                and self._token_expiry
                and datetime.now(UTC) < self._token_expiry - TOKEN_REFRESH_MARGIN
            ):
                return self._token
            await self._login()
            assert self._token is not None
            return self._token
        if self._static_token:
            return self._static_token
        raise OneMapAuthError(
            "No OneMap credentials. Set ONEMAP_TOKEN, or ONEMAP_EMAIL and ONEMAP_PASSWORD."
        )

    async def _login(self) -> None:
        resp = await self._http.post(
            f"{BASE_URL}{AUTH_PATH}",
            json={"email": self._email, "password": self._password},
        )
        if resp.status_code != 200:
            raise OneMapAuthError(f"OneMap login failed (HTTP {resp.status_code}).")
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise OneMapAuthError("OneMap login response did not contain an access_token.")
        self._token = token
        self._token_expiry = self._parse_expiry(data.get("expiry_timestamp"))
        logger.info("OneMap token refreshed; expires %s", self._token_expiry)

    @staticmethod
    def _parse_expiry(raw) -> datetime:
        try:
            return datetime.fromtimestamp(int(raw), tz=UTC)
        except (TypeError, ValueError):
            # Tokens last about three days; fall back to that if the field is missing.
            return datetime.now(UTC) + timedelta(days=3)

    # --- HTTP with retry/refresh -----------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        needs_auth: bool = False,
    ) -> httpx.Response:
        attempt = 0
        refreshed = False
        while True:
            headers = {}
            if needs_auth:
                headers["Authorization"] = await self._auth_token()
            try:
                resp = await self._http.request(method, url, params=params, headers=headers)
            except httpx.TransportError as exc:
                # Timeouts and dropped connections happen under a burst of routing
                # calls; these requests are read-only GETs, so retry with backoff
                # rather than failing the whole plan for one flaky leg.
                if attempt >= self._max_retries:
                    raise
                delay = self._backoff_base * (2**attempt)
                attempt += 1
                logger.warning(
                    "OneMap request failed (%s); retrying in %.1fs",
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 429 and attempt < self._max_retries:
                delay = self._backoff_base * (2**attempt)
                attempt += 1
                logger.warning("OneMap rate limited (429); backing off %.1fs", delay)
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 401 and needs_auth and not refreshed:
                if self._can_refresh():
                    logger.info("OneMap returned 401; refreshing token and retrying once")
                    await self._login()
                    refreshed = True
                    continue
                raise OneMapAuthError(
                    "OneMap rejected the token (401). If you set ONEMAP_TOKEN it has likely "
                    "expired - refresh it, or set ONEMAP_EMAIL and ONEMAP_PASSWORD so the "
                    "client can refresh on its own."
                )

            resp.raise_for_status()
            return resp

    # --- Public API -------------------------------------------------------

    async def geocode(self, query: str) -> GeocodeResult | None:
        """Resolve a postal code, building name, or address to a location.

        Returns ``None`` when there are no results (the endpoint is not typo
        tolerant, so the caller should ask the user to retry).
        """
        resp = await self._request(
            "GET",
            f"{BASE_URL}{SEARCH_PATH}",
            params={
                "searchVal": query,
                "returnGeom": "Y",
                "getAddrDetails": "Y",
                "pageNum": 1,
            },
            needs_auth=True,
        )
        data = resp.json()
        if not isinstance(data, dict) or data.get("error"):
            raise OneMapAuthError("OneMap rejected the search request.")
        results = data.get("results") or []
        if not results:
            return None
        top = results[0]
        try:
            point = LatLng(float(top["LATITUDE"]), float(top["LONGITUDE"]))
        except (KeyError, TypeError, ValueError):
            return None
        label = top.get("SEARCHVAL") or top.get("BUILDING") or top.get("ADDRESS") or query
        return GeocodeResult(label=label, point=point, address=top.get("ADDRESS"))

    async def drive(self, start: LatLng, end: LatLng, depart: datetime) -> float | None:
        """Driving time in minutes, or ``None`` if no route was found."""
        return await self._route("drive", start, end, depart)

    async def pt(self, start: LatLng, end: LatLng, depart: datetime) -> float | None:
        """Public-transport time in minutes (incl. walking), or ``None`` if none found."""
        return await self._route("pt", start, end, depart)

    async def _route(
        self, route_type: str, start: LatLng, end: LatLng, depart: datetime
    ) -> float | None:
        key = (route_type, start.as_param, end.as_param, _round_depart(depart))
        if key in self._cache:
            return self._cache[key]
        async with self._sem:
            if key in self._cache:  # another task may have filled it while we waited
                return self._cache[key]
            params = self._route_params(route_type, start, end, depart)
            try:
                resp = await self._request(
                    "GET", f"{BASE_URL}{ROUTE_PATH}", params=params, needs_auth=True
                )
            except httpx.HTTPStatusError as exc:
                # OneMap answers 404 for unroutable pairs (including start == end);
                # that is "no route found", not an error worth failing the query for.
                if exc.response.status_code == 404:
                    self._cache[key] = None
                    return None
                raise
            minutes = self._parse_route(route_type, resp.json())
            self._cache[key] = minutes
            return minutes

    def _route_params(self, route_type: str, start: LatLng, end: LatLng, depart: datetime) -> dict:
        params: dict = {
            "start": start.as_param,
            "end": end.as_param,
            "routeType": route_type,
        }
        if route_type == "pt":
            params.update(
                {
                    "date": depart.strftime(PT_DATE_FMT),
                    "time": depart.strftime(PT_TIME_FMT),
                    "mode": "TRANSIT",
                    "maxWalkDistance": self._pt_max_walk_distance_m,
                    "numItineraries": self._pt_num_itineraries,
                }
            )
        return params

    @staticmethod
    def _parse_route(route_type: str, data: dict) -> float | None:
        if route_type == "pt":
            itineraries = (data.get("plan") or {}).get("itineraries") or []
            durations = [it.get("duration") for it in itineraries if it.get("duration") is not None]
            if not durations:
                return None
            return seconds_to_minutes(min(durations))
        summary = data.get("route_summary") or {}
        total_time = summary.get("total_time")
        if total_time is None:
            return None
        return seconds_to_minutes(total_time)
