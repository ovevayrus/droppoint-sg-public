"""Shared test helpers: fixture loading and a swappable fake OneMap client."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from droppoint.onemap import GeocodeResult, LatLng

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load a saved OneMap JSON response from tests/fixtures."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeOneMapClient:
    """A high-level stand-in for OneMapClient used by optimiser and bot tests.

    Times are looked up by (start, end) LatLng pairs. A missing pair returns None,
    which models "OneMap found no route" - the optimiser must skip it without crashing.
    """

    def __init__(
        self,
        drive_times: dict[tuple[LatLng, LatLng], float | None] | None = None,
        pt_times: dict[tuple[LatLng, LatLng], float | None] | None = None,
        geocodes: dict[str, GeocodeResult | None] | None = None,
    ) -> None:
        self.drive_times = drive_times or {}
        self.pt_times = pt_times or {}
        self.geocodes = geocodes or {}
        self.calls: list[tuple[str, LatLng, LatLng]] = []

    async def drive(self, start: LatLng, end: LatLng, depart: datetime) -> float | None:
        self.calls.append(("drive", start, end))
        return self.drive_times.get((start, end))

    async def pt(self, start: LatLng, end: LatLng, depart: datetime) -> float | None:
        self.calls.append(("pt", start, end))
        return self.pt_times.get((start, end))

    async def geocode(self, query: str) -> GeocodeResult | None:
        return self.geocodes.get(query.strip().lower())
