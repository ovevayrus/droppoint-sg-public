# DropPoint SG

Find a practical pickup or drop-off point for a Singapore carpool. This Telegram bot compares OneMap driving and public-transport routes, then shows the driver's detour and each passenger's travel time.

It handles one to three passengers, including shared drop-off routes and pickups with suggested departure times. This is a self-hosted prototype with a starter station dataset. Check suggested stopping points and travel times yourself.

In a private **test-bot** chat, send `/start`, then send the shared origin, driver destination and passenger destinations in one message, one place per line. Send `now` or a departure time next. Use `/pickup` when passengers start in different places. Reply examples below are illustrative, not captured Telegram conversations or measured travel-time claims.

Say two people leave from the same place. One is driving to their own destination;
the other is headed somewhere else. DropPoint tests MRT/LRT stations and major
interchanges to find a drop-off that saves the rider time without giving the driver
a silly detour.

## How it works

For each candidate drop-off point P (an MRT/LRT station or major interchange):

```
base_D   = drive(A, B)              base_R   = pt(A, C)
new_D(P) = drive(A, P) + drive(P,B) new_R(P) = drive(A, P) + pt(P, C)
cost_D   = new_D - base_D           save_R   = base_R - new_R
net      = save_R - cost_D
```

A point is eligible only if the rider saves time, the pair saves time, the driver's
detour is within a cap (`MAX_DRIVER_OVERHEAD_FRAC`, default 50%), and the rider's
proportional gain is at least the driver's proportional cost (a fairness rule).
Eligible points are ranked by total minutes saved (`OBJECTIVE=efficient`) or by
fairness (`OBJECTIVE=balanced`).

### Multiple passengers (2–3)

With two or three passengers the bot switches to the joint planner in `multistop.py`.
It plans a single driving route, `A -> stop -> stop -> ... -> B`, instead of scoring
each passenger on their own. A passenger may be dropped at a station, taken to their
door, or told that travelling separately is quicker. The planner also chooses the
stop order.

Multi-passenger mode has no hard cap on the driver's extra time by default. It shows
the detour instead and ranks plans by net minutes saved. Set
`MULTI_MAX_DRIVER_OVERHEAD_FRAC` if you want a cap. The reply also includes a "full
chauffeur" comparison, so it is easy to see what driving everyone home would cost,
along with lower-detour alternatives.

To keep the combinatorial search cheap, legs *between* stops are first estimated with
a haversine speed model; the top plans are then re-costed with real OneMap driving
times before anything is shown.

### Pick-ups (`/pickup`)

The same joint planner also handles pick-ups. The driver goes from A to a shared
destination C while each passenger starts somewhere else. A meeting point can be a
station the passenger reaches by public transport, the passenger's door, or a place
already near the driver's route. The result is one route,
`A -> stop -> ... -> C`.

Pick-up plans also work out when each person should leave. The driver can set off
later while passengers travel to their meeting points, which avoids making people
wait beside the road. The reply shows the suggested delay for the driver and a start
time for each passenger.

Times come from [OneMap](https://www.onemap.gov.sg/apidocs/), the Singapore Land
Authority map service. Before calling the API, a rough geometric filter removes
stations that are well outside the A-to-B corridor. Single-rider scoring needs at most
`MAX_CANDIDATES_EVALUATED × 3 + 2` route legs before retries. Geocoding, authentication, retries and joint planning add work. The explicit attempt allowance below counts every outbound HTTP attempt.

## Quick start

Requires Python 3.11+.

```bash
# 1. Install (a virtual environment is recommended)
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -c constraints.txt -e ".[dev]"

# 2. Configure secrets
cp .env.example .env        # then edit .env

# 3. Run the tests (offline, no network needed)
pytest -q

# 4. Run a separate test bot; polling changes that bot's delivery mode
droppoint-bot               # or: python -m droppoint.bot
```

### Secrets

Copy `.env.example` to `.env` and fill in:

- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- OneMap credentials, either:
  - `ONEMAP_TOKEN` (quick start; expires after ~3 days), or
  - `ONEMAP_EMAIL` + `ONEMAP_PASSWORD` (recommended; the client refreshes its own
    token automatically).

See `.env.example` for the full list and optional tuning knobs. `.env` is gitignored;
never commit it.

### Work limits and safe startup

The supported deployment is one polling process. Defaults are finite demo safeguards, **not** the numerical quota of your OneMap account:

| Variable | Default | What it bounds |
|---|---:|---|
| `MAX_TRIP_REQUESTS` | 300 | Outbound attempts across both phases, including auth/retries |
| `TRIP_WORK_SECONDS` | 180 | Cumulative active work time; waiting for the user is excluded |
| `TRIPS_PER_MINUTE` | 20 | Accepted trip starts per polling process |
| `USER_COOLDOWN_SECONDS` | 60 | Minimum time between one user's location submissions |
| `ONEMAP_REQUESTS_PER_MINUTE` | 600 | Shared outbound attempts, including search/auth/routing |
| `ALLOW_WEBHOOK_RESET` | false | Permission to replace an existing webhook with polling |

Verify your actual OneMap entitlement and lower these limits to match it. A rejected or slow trip gets a retry message, not a partially verified route. All outbound paths share concurrency limits; simultaneous token refreshes share a lock. A long provider `Retry-After` stops the trip instead of retrying too early or sleeping indefinitely.

These in-process counters do not protect multiple instances. Do not run several pollers for the same bot token or claim distributed quota protection. A multiworker/webhook deployment needs a shared limiter.

Startup reads webhook information first and refuses an existing webhook unless `ALLOW_WEBHOOK_RESET=true`. Prefer a separate test bot. The override can interrupt hosted delivery; restoring its prior webhook is an operator action.

### Privacy and logs

The bot accepts trip details only in a private Telegram chat. Locations are sent to
Telegram and OneMap so the bot can resolve places and
calculate routes. DropPoint SG does not write them to its own database: they live in
the bot process's per-user memory during an active trip. They are cleared after a
result, cancellation, or error, and an unfinished trip expires after 15 minutes. Parallel tasks are cancelled and awaited before cleanup; each active phase owns its short-lived coordinate cache. Admission IDs are retained only through the cooldown window.
Telegram, OneMap, and your hosting provider have their own retention policies.
Opening a result's Google Maps link sends its route coordinates to Google.

Production logging deliberately suppresses HTTP request URLs because Telegram URLs
contain the bot token and OneMap URLs can contain addresses or coordinates. Do not
enable HTTPX debug or INFO logging in production without adding equivalent redaction.

## Using the bot

In a private chat, send `/start`, then send all three to five places in one message: starting point, driver destination and passenger destinations, one per line. Send a departure time (or "now") separately. Each location
can be a postal code, building name, or address. The bot replies with the best
drop-off, up to two alternatives, and a Google Maps link:

```
Best drop-off: Bishan MRT (NS / CC)

You drive:  20 min  ->  27 min  (+7)
Friend:     90 min  ->  58 min  (-32)
Saved together: 25 min

Map: https://www.google.com/maps/dir/?api=1&origin=...&destination=...&waypoints=...

Other options:
2) Toa Payoh MRT   you +5, friend -24
```

With two or three passengers the reply is a set of route options instead:

```
4 route options for you and 2 passengers:

Option 1 - best overall, most passenger time saved
You drive: 20 -> 35 min (+15)
Route: Serangoon Central -> Tampines St 82 -> your destination
- Passenger 1: dropped at destination - 90 -> 18 min (-72)
- Passenger 2: dropped at destination - 70 -> 25 min (-45)
Together you save 102 min.
Map: https://www.google.com/maps/dir/?api=1&origin=...&waypoints=...%7C...

Option 2 - least detour for you
...
```

`/pickup` starts pick-up mode instead: line 1 is where the driver starts, line 2 the
destination everyone shares, lines 3–5 where each passenger is coming from. The reply
shows meeting points with waiting/leave-later times per passenger.

`/cancel` drops the current flow. `/help` repeats the instructions. If no drop-off is
worth it, the bot says travelling separately is best and shows both baseline times.

## Architecture

The code is split across these modules:

| Module | Responsibility |
|--------|----------------|
| `config.py` | `Settings` (env / `.env`) and a secret-free `OptimiserConfig` |
| `onemap.py` | `OneMapClient`: auth, geocoding, drive + PT routing, caching, retries. The only place that touches HTTP. Swappable via the `RouteClient` protocol. |
| `stations.py` | Station dataset loader and the haversine corridor prefilter (no API calls) |
| `optimiser.py` | Pure scoring/ranking (`score_candidate`, `rank_candidates`) and the async `optimise` orchestration (single rider) |
| `multistop.py` | Joint planner for 1–3 passengers in both directions: drop-offs (`optimise_multi`) and pick-ups with meeting-time sync (`optimise_pickup`); option menus per rider, one shared stop order, estimate-then-verify costing |
| `formatting.py` | Reply text and Google Maps links |
| `bot.py` | Telegram conversation flow; injects the client, stations, and config via `bot_data` |
| `data/stations.json` | Candidate MRT/LRT stations and interchanges (a starter set; grow it) |

Data flow for one query: `bot` → `optimise` → baselines + `corridor_prefilter` →
concurrent OneMap calls → `score_candidate`/`rank_candidates` → `format_result` → reply.

The optimiser core is pure (times in, ranked results out), so the whole test suite
runs offline against a mock client and saved fixtures.

The bundled station list is a project-maintained starter dataset with approximate
station-centre coordinates, not an official transport dataset. Verify additions
against an authoritative source and follow that source's reuse terms.

## Development

```bash
pytest -q                                  # offline tests
pytest --cov=droppoint --cov-report=term-missing   # with coverage
ruff check .                               # lint
black .                                    # format
pytest -m integration                      # real OneMap calls (needs credentials; off by default)
```

### Manual smoke check (against the real OneMap API)

With credentials in `.env`, confirm the bot end to end:

1. `droppoint-bot` to start polling.
2. In Telegram, message your bot `/start`.
3. Send `Jurong East MRT`, `Tampines` and `Somerset` in one message, one place per line; then send `now` separately. Use public places rather than private addresses in demonstrations.
4. You should get a best drop-off with driver/rider times and a Google Maps link.

A quick non-Telegram check of the OneMap client and optimiser:

```python
import asyncio
from datetime import datetime, timedelta, timezone
from droppoint.config import get_settings
from droppoint.onemap import LatLng, OneMapClient
from droppoint.optimiser import optimise
from droppoint.stations import load_stations

async def main():
    client = OneMapClient.from_settings(get_settings())
    sgt = timezone(timedelta(hours=8))
    res = await optimise(
        client,
        origin=LatLng(1.3331, 103.7422),     # Jurong East
        driver_dest=LatLng(1.3536, 103.9450), # Tampines
        rider_dest=LatLng(1.3007, 103.8388),  # Somerset
        stations=load_stations(),
        depart=datetime.now(sgt),
        config=get_settings().optimiser(),
    )
    print(res.best.name if res.best else "travel separately")
    await client.aclose()

asyncio.run(main())
```

## Contributing

See `CONTRIBUTING.md`. Good first issues: grow `data/stations.json`, improve the
prefilter, or add a OneMap static map image to replies.

## Data attribution

This application uses search and routing information from
[OneMap](https://www.onemap.gov.sg/), provided by the Singapore Land Authority.
OneMap information is accessed at runtime and made available under the
[Singapore Open Data Licence version 1.0](https://www.onemap.gov.sg/legal/opendatalicence.html).
Use of the API is also subject to OneMap's
[API Terms of Service](https://www.onemap.gov.sg/legal/apitermsofservice.html).

## Licence

MIT. See `LICENSE`.
