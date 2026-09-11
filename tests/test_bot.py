"""Bot conversation tests using fake Update/Context objects and the fake OneMap client.

These exercise the handlers directly (no live Telegram) and assert reply contents and
the conversation state transitions.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import InvalidToken
from telegram.ext import ConversationHandler

import droppoint.bot as bot_module
from droppoint.bot import (
    ASK_ALL,
    ASK_DEPART,
    CONVERSATION_TIMEOUT_SECONDS,
    HELP,
    PRIVACY,
    _parse_depart,
    _parse_tokens,
    build_application,
    cancel,
    configure_logging,
    error_handler,
    expire_conversation,
    help_command,
    main,
    privacy_command,
    received_all,
    received_depart,
    start,
)
from droppoint.config import OptimiserConfig, Settings
from droppoint.onemap import GeocodeResult, LatLng
from droppoint.stations import Station

A = LatLng(1.30, 103.80)
B = LatLng(1.35, 103.90)
C = LatLng(1.32, 103.88)

S1 = Station(name="Bishan MRT", lat=1.34, lng=103.88, lines=["NS", "CC"])
S2 = Station(name="Toa Payoh MRT", lat=1.32, lng=103.84, lines=["NS"])
S3 = Station(name="No Route MRT", lat=1.345, lng=103.89, lines=["XX"])

CONFIG = OptimiserConfig(corridor_max_offset_km=50.0, max_candidates_evaluated=15)

GEOCODES = {
    "start": GeocodeResult(label="Start Point", point=A),
    "driver": GeocodeResult(label="Driver Dest", point=B),
    "rider": GeocodeResult(label="Rider Dest", point=C),
    "rider2": GeocodeResult(label="Rider 2 Dest", point=LatLng(1.33, 103.86)),
}


class FakeClient:
    """Fake OneMap client with geocoding and route times."""

    def __init__(self, with_legs: bool = True, with_baselines: bool = True):
        self.geocodes = GEOCODES
        self.drive = AsyncMock(side_effect=self._drive)
        self.pt = AsyncMock(side_effect=self._pt)
        self.clear_cache_calls = 0
        self._drive_times = {}
        self._pt_times = {}
        if with_baselines:
            self._drive_times[(A, B)] = 20.0
            self._pt_times[(A, C)] = 90.0
        if with_legs:
            self._drive_times.update(
                {(A, S1.point): 10.0, (S1.point, B): 17.0, (A, S2.point): 7.0, (S2.point, B): 18.0}
            )
            self._pt_times.update({(S1.point, C): 48.0, (S2.point, C): 59.0})

    async def _drive(self, start, end, depart):
        return self._drive_times.get((start, end))

    async def _pt(self, start, end, depart):
        return self._pt_times.get((start, end))

    async def geocode(self, query):
        return self.geocodes.get(query.strip().lower())

    def clear_cache(self):
        self.clear_cache_calls += 1


def make_context(client):
    return SimpleNamespace(
        bot_data={"client": client, "stations": [S1, S2, S3], "config": CONFIG},
        user_data={},
    )


class Recorder:
    """Builds fake updates and records every reply sent."""

    def __init__(self):
        self.replies: list[str] = []

    def update(self, text=None, location=None):
        replies = self.replies

        async def reply_text(t, **kwargs):
            replies.append(t)

        chat = SimpleNamespace(send_action=AsyncMock())
        message = SimpleNamespace(text=text, location=location, reply_text=reply_text)
        return SimpleNamespace(message=message, effective_chat=chat)


# --- helpers ------------------------------------------------------------------


def test_parse_depart():
    assert _parse_depart("now") is not None
    assert _parse_depart("") is not None
    assert _parse_depart("18:30").hour == 18
    assert _parse_depart("18:30").minute == 30
    assert _parse_depart("7:05 am").hour == 7
    assert _parse_depart("banana") is None


def test_parse_tokens():
    assert _parse_tokens("111111\n222222\n333333") == ["111111", "222222", "333333"]
    assert _parse_tokens("111111,222222,333333") == ["111111", "222222", "333333"]
    assert _parse_tokens("  111111  \n\n  222222  ") == ["111111", "222222"]


# --- full flow ----------------------------------------------------------------


async def test_full_flow_single_rider():
    client = FakeClient()
    context = make_context(client)
    rec = Recorder()

    assert await start(rec.update(text="/start"), context) == ASK_ALL
    assert await received_all(rec.update(text="start\ndriver\nrider"), context) == ASK_DEPART
    assert await received_depart(rec.update(text="now"), context) == ConversationHandler.END

    final = rec.replies[-1]
    assert "Best drop-off: Bishan MRT" in final
    assert "You drive:  20 min  ->  27 min  (+7)" in final
    assert "Friend:     90 min  ->  58 min  (-32)" in final
    assert "Saved together: 25 min" in final
    assert "Map: https://www.google.com/maps/dir/" in final
    assert "Toa Payoh MRT" in final
    assert "No Route MRT" not in final
    assert context.user_data == {}
    assert client.clear_cache_calls >= 2


async def test_too_few_codes_asks_again():
    client = FakeClient()
    context = make_context(client)
    rec = Recorder()
    state = await received_all(rec.update(text="start\ndriver"), context)
    assert state == ASK_ALL
    assert "at least 3" in rec.replies[-1]


async def test_too_many_codes_asks_again():
    client = FakeClient()
    context = make_context(client)
    rec = Recorder()
    state = await received_all(rec.update(text="a\nb\nc\nd\ne\nf"), context)
    assert state == ASK_ALL
    assert "up to 5" in rec.replies[-1]


async def test_unknown_location_asks_again():
    client = FakeClient()
    context = make_context(client)
    rec = Recorder()
    state = await received_all(rec.update(text="start\nnowhere unknown\nrider"), context)
    assert state == ASK_ALL
    assert "couldn't find" in rec.replies[-1]


async def test_invalid_time_asks_again():
    client = FakeClient()
    context = make_context(client)
    context.user_data.update(
        {"origin": (A, "Start"), "driver_dest": (B, "Driver"), "riders": [(C, "Rider")]}
    )
    rec = Recorder()
    state = await received_depart(rec.update(text="banana o'clock"), context)
    assert state == ASK_DEPART
    assert "didn't catch that time" in rec.replies[-1]


async def test_no_good_option_message():
    client = FakeClient(with_legs=False)
    context = make_context(client)
    context.user_data.update(
        {"origin": (A, "Start"), "driver_dest": (B, "Driver"), "riders": [(C, "Rider")]}
    )
    rec = Recorder()
    state = await received_depart(rec.update(text="now"), context)
    assert state == ConversationHandler.END
    assert "Travelling separately is best here." in rec.replies[-1]


async def test_cancel_and_help():
    client = FakeClient()
    context = make_context(client)
    rec = Recorder()

    assert await cancel(rec.update(text="/cancel"), context) == ConversationHandler.END
    assert "Cancelled" in rec.replies[-1]

    await help_command(rec.update(text="/help"), context)
    assert rec.replies[-1] == HELP

    await privacy_command(rec.update(text="/privacy"), context)
    assert rec.replies[-1] == PRIVACY


async def test_location_lookup_failure_clears_state_without_logging_input(caplog):
    private_input = "private-address=123 Example Street"

    class BoomGeocoder(FakeClient):
        async def geocode(self, query):
            raise RuntimeError(private_input)

    client = BoomGeocoder()
    context = make_context(client)
    context.user_data["mode"] = "dropoff"
    rec = Recorder()
    with caplog.at_level(logging.ERROR):
        state = await received_all(rec.update(text=f"{private_input}\ndriver\nrider"), context)

    assert state == ConversationHandler.END
    assert context.user_data == {}
    assert "Something went wrong" in rec.replies[-1]
    assert private_input not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_failed_confirmation_never_stores_resolved_locations(caplog):
    client = FakeClient()
    context = make_context(client)
    context.user_data["mode"] = "dropoff"
    update = Recorder().update(text="start\ndriver\nrider")
    update.message.reply_text = AsyncMock(
        side_effect=RuntimeError("private-address=123 Example Street")
    )

    with caplog.at_level(logging.ERROR):
        state = await received_all(update, context)

    assert state == ConversationHandler.END
    assert context.user_data == {}
    assert "private-address" not in caplog.text


async def test_expired_conversation_clears_locations_and_cache():
    client = FakeClient()
    context = make_context(client)
    context.user_data.update({"origin": (A, "Private Home"), "driver_dest": (B, "Work")})
    rec = Recorder()
    update = rec.update(text="now")
    update.effective_message = update.message

    state = await expire_conversation(update, context)

    assert state == ConversationHandler.END
    assert context.user_data == {}
    assert client.clear_cache_calls == 1
    assert "expired" in rec.replies[-1]


async def test_global_error_handler_redacts_exception_and_clears_state(caplog):
    private_detail = "private-address=123 Example Street"
    client = FakeClient()
    context = make_context(client)
    context.user_data["origin"] = (A, "Private Home")
    context.error = RuntimeError(private_detail)

    with caplog.at_level(logging.ERROR):
        await error_handler(None, context)

    assert context.user_data == {}
    assert private_detail not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_optimiser_failure_sends_friendly_message_without_leaking_state(caplog):
    class Boom:
        async def geocode(self, query):
            return None

        async def drive(self, *args):
            raise RuntimeError("private-address=123 Example Street")

        async def pt(self, *args):
            raise RuntimeError("private-address=123 Example Street")

    context = SimpleNamespace(
        bot_data={"client": Boom(), "stations": [S1], "config": CONFIG},
        user_data={"origin": (A, "S"), "driver_dest": (B, "D"), "riders": [(C, "R")]},
    )
    rec = Recorder()
    with caplog.at_level(logging.ERROR):
        state = await received_depart(rec.update(text="now"), context)
    assert state == ConversationHandler.END
    assert "Something went wrong" in rec.replies[-1]
    assert context.user_data == {}
    assert "private-address" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_http_request_urls_are_not_logged_at_info():
    configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("telegram").getEffectiveLevel() >= logging.WARNING


def test_invalid_token_library_log_is_redacted(caplog):
    private_token = "private-token-value"
    configure_logging()
    with caplog.at_level(logging.ERROR, logger="telegram.ext"):
        try:
            raise InvalidToken(f"The token {private_token} was rejected")
        except InvalidToken:
            logging.getLogger("telegram.ext").exception("Invalid token")

    assert private_token not in caplog.text
    assert "configured bot token" in caplog.text


async def test_polling_error_context_without_user_data_is_safe(caplog):
    private_detail = "private-token-value"
    client = FakeClient()
    context = SimpleNamespace(
        bot_data={"client": client},
        user_data=None,
        error=RuntimeError(private_detail),
    )

    with caplog.at_level(logging.ERROR):
        await error_handler(None, context)

    assert client.clear_cache_calls == 1
    assert private_detail not in caplog.text
    assert "RuntimeError" in caplog.text


def test_settings_repr_redacts_credentials():
    settings = Settings(
        onemap_email="person@example.test",
        onemap_password="not-a-real-password",  # pragma: allowlist secret
        telegram_bot_token="test-token-not-real",
    )
    rendered = repr(settings)
    assert "person@example.test" not in rendered
    assert "not-a-real-password" not in rendered
    assert "test-token-not-real" not in rendered


def test_build_application_wires_dependencies():
    settings = Settings(telegram_bot_token="test-token-not-real")
    client = FakeClient()
    app = build_application(settings, client)
    assert app.bot_data["client"] is client
    assert len(app.bot_data["stations"]) > 0
    assert app.bot_data["config"] is not None
    conversation = app.handlers[0][0]
    assert isinstance(conversation, ConversationHandler)
    assert conversation.conversation_timeout == CONVERSATION_TIMEOUT_SECONDS
    assert ConversationHandler.TIMEOUT in conversation.states
    assert app.job_queue is not None


def test_main_redacts_rejected_telegram_token(monkeypatch, capsys):
    private_token = "private-token-value"
    settings = Settings(telegram_bot_token=private_token, onemap_token="test-token")

    class RejectingApplication:
        def run_polling(self):
            raise InvalidToken(f"The token {private_token} was rejected")

    monkeypatch.setattr(bot_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        bot_module,
        "OneMapClient",
        SimpleNamespace(from_settings=lambda _settings: FakeClient()),
    )
    monkeypatch.setattr(
        bot_module, "build_application", lambda _settings, _client: RejectingApplication()
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    output = capsys.readouterr()
    assert private_token not in str(exc_info.value)
    assert private_token not in output.out
    assert private_token not in output.err
