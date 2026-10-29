# Contributing to DropPoint SG

Use `pip install -c constraints.txt -e ".[dev]"` for the tested dependency resolution. New request/planner code must preserve cancellation cleanup, attempt/deadline accounting and safe logging; add delayed/failing mock-transport cases instead of live provider traffic. Test bots and accounts must be separate from production.

Thanks for your interest. This is a small, focused project. Contributions that keep
it simple are very welcome.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Before you open a change

- `pytest -q` passes (offline; no network required).
- `ruff check .` is clean.
- `black .` reports no changes.

Tests must run offline by default. Any real OneMap call belongs behind the
`integration` pytest marker, which is off by default.

## Good first contributions

- Grow `data/stations.json` with more stations and major bus interchanges.
- Improve the geometric prefilter.
- Add a OneMap static map image to the reply.

## Conventions

- User-facing strings and docs use British English and plain language.
- Keep dependencies minimal. Explain non-obvious additions in the pull request.
- Never commit secrets. Keep them in `.env` (gitignored).
- Small, working commits. Do not commit while tests fail.

## Current scope

The bot handles one driver and up to three passengers in either drop-off or pick-up
mode. It is a carpool planner, not a general fleet-routing system.
