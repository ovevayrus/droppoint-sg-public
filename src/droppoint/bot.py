"""Telegram bot: send all postal codes in one message, then a departure time.

Flow: /start -> send all postal codes (one per line, 3–5 total) -> departure time -> result.
First code = shared origin; second = driver's destination; remaining = passenger drop-offs.
/cancel drops the flow; /help repeats the instructions.

Postal codes may also be addresses; each is geocoded through OneMap.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import InvalidToken
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import get_settings
from .formatting import format_multi_result, format_plan_options
from .limits import Admission, TripWork, WorkLimitExceeded, gather_cancel, trip_scope
from .multistop import optimise_multi, optimise_pickup
from .onemap import LatLng, OneMapClient
from .optimiser import optimise
from .stations import default_stations

logger = logging.getLogger(__name__)

SINGAPORE_TZ = timezone(timedelta(hours=8))

ASK_ALL, ASK_DEPART = range(2)
CONVERSATION_TIMEOUT_SECONDS = 15 * 60


class _RedactInvalidToken(logging.Filter):
    """Remove Telegram credentials from the library's InvalidToken log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        error = record.exc_info[1] if record.exc_info else None
        if isinstance(error, InvalidToken):
            record.msg = "Telegram rejected the configured bot token."
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


INVALID_TOKEN_LOG_FILTER = _RedactInvalidToken()


def configure_logging() -> None:
    """Configure useful application logs without recording request URLs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # HTTPX logs full URLs at INFO. Telegram places the bot token in its API URL,
    # while OneMap URLs can contain addresses and coordinates.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    telegram_ext_logger = logging.getLogger("telegram.ext")
    if INVALID_TOKEN_LOG_FILTER not in telegram_ext_logger.filters:
        telegram_ext_logger.addFilter(INVALID_TOKEN_LOG_FILTER)
    for handler in logging.getLogger().handlers:
        if INVALID_TOKEN_LOG_FILTER not in handler.filters:
            handler.addFilter(INVALID_TOKEN_LOG_FILTER)


INTRO = (
    "Hi! Send your postal codes, one per line:\n\n"
    "Line 1 — where everyone is starting from\n"
    "Line 2 — your destination (as the driver)\n"
    "Line 3–5 — each passenger's destination (up to 3)\n\n"
    "You need at least 3 lines (you + one passenger). Send up to 5.\n"
    "(Picking people up on the way somewhere instead? Use /pickup.)"
)

PICKUP_INTRO = (
    "Pick-up mode! Send your postal codes, one per line:\n\n"
    "Line 1 — where you are starting from (as the driver)\n"
    "Line 2 — the destination you are all going to\n"
    "Line 3–5 — where each passenger is coming from (up to 3)\n\n"
    "You need at least 3 lines (you + one passenger). Send up to 5.\n"
    "I'll find meeting points - a station near them, their door, or somewhere\n"
    "on your way - so you can collect them without a big detour."
)

HELP = (
    "DropPoint SG plans carpool routes with smart drop-off and pick-up points.\n\n"
    "/start — drop-off mode: everyone starts together, you drive, and I find the\n"
    "best places to drop each passenger.\n"
    "  Line 1: shared starting point\n"
    "  Line 2: your destination\n"
    "  Lines 3–5: each passenger's destination (1–3 passengers)\n\n"
    "/pickup — pick-up mode: you drive to a shared destination and collect\n"
    "passengers on the way (at a station they can reach by public transport,\n"
    "or at their door).\n"
    "  Line 1: your starting point\n"
    "  Line 2: the shared destination\n"
    "  Lines 3–5: each passenger's starting point (1–3 passengers)\n\n"
    "Then send a departure time ('now' or e.g. 18:30).\n\n"
    "You get several route options - station stops, door-to-door, or a mix -\n"
    "ranked by total time saved. Your extra driving time is always shown, and\n"
    "there is no hard cap on it: you decide. Pick-up options include meeting\n"
    "points that cost the group a few minutes but cut your detour a lot -\n"
    "collecting people is the point - with the trade always shown. Pick-up\n"
    "plans also synchronise departures so nobody waits: each option says how\n"
    "much later you and each passenger should set off to arrive at the\n"
    "meeting points together.\n\n"
    "/cancel stops the current plan. /privacy explains how trip data is handled.\n\n"
    "Route data: OneMap, Singapore Land Authority."
)

PRIVACY = (
    "Use this bot in a private chat. Trip locations pass through Telegram and are "
    "sent to OneMap for search and routing. DropPoint SG keeps them in memory only "
    "during an active trip, clears them after a result, cancellation, or error, and "
    "expires an unfinished trip after 15 minutes. Opening a Google Maps link sends the "
    "route coordinates to Google."
)


def _split_message(text: str, limit: int = 3500) -> list[str]:
    """Split a long reply on blank lines so each part fits Telegram's 4096-char cap."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            parts.append(current)
            current = block
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _parse_tokens(text: str) -> list[str]:
    """Split a newline- or comma-separated message into individual location tokens."""
    tokens = []
    for part in text.replace(",", "\n").splitlines():
        t = part.strip()
        if t:
            tokens.append(t)
    return tokens


async def _geocode_all(client, tokens: list[str]) -> list[tuple[LatLng, str] | None]:
    """Geocode every token concurrently. Returns None entries for failures."""

    async def _one(token: str) -> tuple[LatLng, str] | None:
        geo = await client.geocode(token)
        if geo is None:
            return None
        return geo.point, geo.label

    return await gather_cancel(*(_one(t) for t in tokens))


def _parse_depart(text: str) -> datetime | None:
    """Parse 'now' or a clock time into a Singapore-time datetime. None if invalid."""
    cleaned = text.strip().lower()
    now = datetime.now(SINGAPORE_TZ)
    if cleaned in ("", "now"):
        return now
    for fmt in ("%H:%M", "%H.%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            parsed = datetime.strptime(cleaned.upper(), fmt)
        except ValueError:
            continue
        return now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    return None


def _clear_trip_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forget submitted locations and any cached route coordinates."""
    if context.user_data is not None:
        context.user_data.clear()
    bot_data = context.bot_data or {}
    clear_cache = getattr(bot_data.get("client"), "clear_cache", None)
    if callable(clear_cache):
        clear_cache()


def _new_work(context: ContextTypes.DEFAULT_TYPE) -> TripWork:
    limits = context.bot_data.get("work_limits", {})
    return TripWork(
        max_attempts=limits.get("requests", 300),
        remaining_seconds=limits.get("seconds", 180.0),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_trip_data(context)
    context.user_data["mode"] = "dropoff"
    await update.message.reply_text(INTRO)
    return ASK_ALL


async def pickup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_trip_data(context)
    context.user_data["mode"] = "pickup"
    await update.message.reply_text(PICKUP_INTRO)
    return ASK_ALL


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP)


async def privacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(PRIVACY)


async def expire_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_trip_data(context)
    message = update.effective_message
    if message is not None:
        await message.reply_text(
            "That unfinished trip expired, so I cleared its locations. Send /start "
            "or /pickup to begin again."
        )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_trip_data(context)
    await update.message.reply_text("Cancelled. Send /start to plan another trip.")
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear trip data without logging exception text, URLs, or user input."""
    logger.error("Unhandled bot error (%s)", type(context.error).__name__)
    _clear_trip_data(context)


async def received_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    tokens = _parse_tokens(text)

    if len(tokens) < 3:
        await update.message.reply_text(
            "Please send at least 3 postal codes (origin, your destination, and one "
            "passenger's destination), one per line."
        )
        return ASK_ALL

    if len(tokens) > 5:
        await update.message.reply_text(
            f"I can handle up to 5 postal codes (origin + driver + 3 passengers). "
            f"You sent {len(tokens)} — please trim to 5."
        )
        return ASK_ALL

    try:
        chat = update.effective_chat
        if chat is not None:
            await chat.send_action(ChatAction.TYPING)
        admission = context.bot_data.get("admission")
        user_id = getattr(getattr(update, "effective_user", None), "id", None)
        if admission is not None and user_id is not None:
            admission.claim(user_id)
        work = _new_work(context)
        async with trip_scope(work):
            resolved = await _geocode_all(context.bot_data["client"], tokens)
    except (WorkLimitExceeded, TimeoutError):
        _clear_trip_data(context)
        await update.message.reply_text("This trip reached a work limit. Please try again later.")
        return ConversationHandler.END
    except Exception as exc:  # noqa: BLE001
        logger.error("Location lookup failed (%s)", type(exc).__name__)
        _clear_trip_data(context)
        await update.message.reply_text(
            "Something went wrong while looking up those locations. Please start again."
        )
        return ConversationHandler.END

    failed = [tokens[i] for i, r in enumerate(resolved) if r is None]
    if failed:
        lines = ["I couldn't find these locations:"]
        lines.extend(f"  • {f}" for f in failed)
        lines.append("Please fix them and send all postal codes again.")
        await update.message.reply_text("\n".join(lines))
        return ASK_ALL

    origin_label = resolved[0][1]
    dest_label = resolved[1][1]
    n_riders = len(resolved) - 2
    rider_word = "passenger" if n_riders == 1 else "passengers"

    if context.user_data.get("mode") == "pickup":
        summary = (
            f"You start at: {origin_label}\n"
            f"Shared destination: {dest_label}\n"
            f"Picking up {n_riders} {rider_word} on the way."
        )
    else:
        summary = (
            f"Origin: {origin_label}\n"
            f"Your destination: {dest_label}\n"
            f"{n_riders} {rider_word} confirmed."
        )

    try:
        await update.message.reply_text(
            f"{summary}\n\nWhen are you leaving? Send 'now' or a time like 18:30."
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Location confirmation failed (%s)", type(exc).__name__)
        _clear_trip_data(context)
        return ConversationHandler.END

    # Store coordinates only after Telegram confirms the next step. If the reply
    # fails, the locals above fall out of scope without entering conversation state.
    context.user_data["origin"] = resolved[0]
    context.user_data["driver_dest"] = resolved[1]
    context.user_data["riders"] = resolved[2:]
    context.user_data["_trip_work"] = work
    return ASK_DEPART


async def received_depart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    depart = _parse_depart(update.message.text or "")
    if depart is None:
        await update.message.reply_text(
            "I didn't catch that time. Send 'now' or a time like 18:30."
        )
        return ASK_DEPART

    mode = context.user_data.get("mode")
    origin, _ = context.user_data["origin"]
    driver_dest, _ = context.user_data["driver_dest"]
    riders: list[tuple[LatLng, str]] = context.user_data["riders"]
    work = context.user_data.get("_trip_work") or _new_work(context)
    _clear_trip_data(context)

    try:
        async with trip_scope(work):
            chat = update.effective_chat
            if chat is not None:
                await chat.send_action(ChatAction.TYPING)
            if mode == "pickup":
                # Pick-ups: the driver heads to a shared destination and collects
                # passengers en route; the joint planner handles any rider count.
                result = await optimise_pickup(
                    context.bot_data["client"],
                    origin=origin,
                    shared_dest=driver_dest,
                    rider_origins=[point for point, _ in riders],
                    stations=context.bot_data["stations"],
                    depart=depart,
                    config=context.bot_data["config"],
                    rider_labels=[label for _, label in riders],
                )
                text = format_plan_options(
                    result,
                    origin=origin,
                    driver_dest=driver_dest,
                    rider_labels=[label for _, label in riders],
                    depart=depart,
                )
            elif len(riders) == 1:
                rider_dest, _ = riders[0]
                result = await optimise(
                    context.bot_data["client"],
                    origin=origin,
                    driver_dest=driver_dest,
                    rider_dest=rider_dest,
                    stations=context.bot_data["stations"],
                    depart=depart,
                    config=context.bot_data["config"],
                )
                text = format_multi_result(
                    [(result, riders[0])], origin=origin, driver_dest=driver_dest
                )
            else:
                # Two or more passengers: plan one shared route with ordered drop-offs.
                result = await optimise_multi(
                    context.bot_data["client"],
                    origin=origin,
                    driver_dest=driver_dest,
                    rider_dests=[point for point, _ in riders],
                    stations=context.bot_data["stations"],
                    depart=depart,
                    config=context.bot_data["config"],
                    rider_labels=[label for _, label in riders],
                )
                text = format_plan_options(
                    result,
                    origin=origin,
                    driver_dest=driver_dest,
                    rider_labels=[label for _, label in riders],
                    depart=depart,
                )
    except (WorkLimitExceeded, TimeoutError):
        await update.message.reply_text("This trip reached a work limit. Please try again later.")
        return ConversationHandler.END
    except Exception as exc:  # noqa: BLE001
        # HTTP client exceptions can include the full request URL. Log only the
        # exception class so credentials and submitted locations stay private.
        logger.error("Optimiser failed (%s)", type(exc).__name__)
        await update.message.reply_text(
            "Something went wrong while planning. Please try again in a moment."
        )
        return ConversationHandler.END
    finally:
        _clear_trip_data(context)

    for chunk in _split_message(text):
        await update.message.reply_text(chunk, disable_web_page_preview=True)
    return ConversationHandler.END


def build_application(settings, client) -> Application:
    """Wire up the Application with handlers and injected dependencies."""
    token = settings.telegram_bot_token_value
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")

    async def guard_delivery_mode(application):
        webhook = await application.bot.get_webhook_info()
        if webhook.url and not settings.allow_webhook_reset:
            raise RuntimeError(
                "This bot already has a webhook. Use a separate test bot, or explicitly "
                "set ALLOW_WEBHOOK_RESET=true to switch this bot to polling."
            )

    app = Application.builder().token(token).post_init(guard_delivery_mode).build()
    app.bot_data["client"] = client
    app.bot_data["stations"] = list(default_stations())
    app.bot_data["config"] = settings.optimiser()
    app.bot_data["admission"] = Admission(settings.trips_per_minute, settings.user_cooldown_seconds)
    app.bot_data["work_limits"] = {
        "requests": settings.max_trip_requests,
        "seconds": settings.trip_work_seconds,
    }

    private_chat = filters.ChatType.PRIVATE
    text_only = private_chat & filters.TEXT & ~filters.COMMAND
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start, filters=private_chat),
            CommandHandler("pickup", pickup_start, filters=private_chat),
        ],
        states={
            ASK_ALL: [MessageHandler(text_only, received_all)],
            ASK_DEPART: [MessageHandler(text_only, received_depart)],
            ConversationHandler.TIMEOUT: [MessageHandler(private_chat, expire_conversation)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel, filters=private_chat),
            CommandHandler("help", help_command, filters=private_chat),
            CommandHandler("privacy", privacy_command, filters=private_chat),
        ],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        allow_reentry=True,
    )
    app.add_handler(conversation)
    app.add_handler(CommandHandler("help", help_command, filters=private_chat))
    app.add_handler(CommandHandler("privacy", privacy_command, filters=private_chat))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    configure_logging()
    settings = get_settings()
    if not settings.telegram_bot_token_value:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Add it to your .env (see .env.example).")
    if not settings.has_onemap_credentials:
        raise SystemExit(
            "No OneMap credentials. Set ONEMAP_TOKEN, or ONEMAP_EMAIL and ONEMAP_PASSWORD, "
            "in your .env (see .env.example)."
        )

    client = OneMapClient.from_settings(settings)
    app = build_application(settings, client)
    logger.info("DropPoint SG bot starting (polling)")
    try:
        app.run_polling()
    except InvalidToken:
        raise SystemExit(
            "Telegram rejected TELEGRAM_BOT_TOKEN; replace it and try again."
        ) from None


if __name__ == "__main__":
    main()
