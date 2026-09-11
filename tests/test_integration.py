"""Real OneMap calls. Off by default - run with `pytest -m integration`.

These need live credentials in the environment / .env. They are excluded by the
default `addopts = -m 'not integration'` so the normal suite stays fully offline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from droppoint.config import get_settings
from droppoint.onemap import LatLng, OneMapClient
from droppoint.optimiser import optimise
from droppoint.stations import load_stations

pytestmark = pytest.mark.integration

SGT = timezone(timedelta(hours=8))
JURONG_EAST = LatLng(1.3331, 103.7422)
TAMPINES = LatLng(1.3536, 103.9450)
SOMERSET = LatLng(1.3007, 103.8388)


def _client_or_skip() -> OneMapClient:
    settings = get_settings()
    if not settings.has_onemap_credentials:
        pytest.skip("no OneMap credentials configured")
    return OneMapClient.from_settings(settings)


async def test_live_geocode():
    client = _client_or_skip()
    try:
        result = await client.geocode("313 Orchard Road")
        assert result is not None
        assert 1.2 < result.point.lat < 1.5
    finally:
        await client.aclose()


async def test_live_drive_and_pt():
    client = _client_or_skip()
    depart = datetime.now(SGT)
    try:
        drive = await client.drive(JURONG_EAST, TAMPINES, depart)
        pt = await client.pt(JURONG_EAST, SOMERSET, depart)
        assert drive is not None and drive > 0
        assert pt is not None and pt > 0
    finally:
        await client.aclose()


async def test_live_optimise_returns_result():
    client = _client_or_skip()
    try:
        result = await optimise(
            client,
            origin=JURONG_EAST,
            driver_dest=TAMPINES,
            rider_dest=SOMERSET,
            stations=load_stations(),
            depart=datetime.now(SGT),
            config=get_settings().optimiser(),
        )
        assert result.base_d_min > 0
        assert result.base_r_min > 0
    finally:
        await client.aclose()
