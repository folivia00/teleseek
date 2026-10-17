"""
TeleSeek bot entry point.
Initialises Bot + Dispatcher, wires routers and middleware, and starts polling.
"""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import settings
from bot.database.connection import AsyncSessionFactory, close_db
from bot.handlers import admin, feedback, search, start
from bot.middlewares.analytics import UserStatsMiddleware
from bot.middlewares.db import DbSessionMiddleware
from bot.services import formatter

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def on_startup(bot: Bot) -> None:
    """Bot startup actions."""
    logger.info("Bot %s is starting...", settings.bot_name)

    # Check the DB connection with a trivial query.
    # The DB is a hard dependency: without it the bot would silently fail
    # every search request. So on a connection failure we do NOT carry on;
    # we exit with a non-zero code so that Docker (restart: unless-stopped)
    # restarts the container and the logs show a clear cause.
    try:
        from sqlalchemy import text
        async with AsyncSessionFactory() as session:
            await session.execute(text("SELECT 1"))
        logger.info("DB connection: OK")
    except Exception as e:
        logger.critical(
            "Could not connect to the database (health-check SELECT 1 failed): %s. "
            "Stopping the bot: running without a database is not allowed.",
            e,
        )
        # Best-effort admin notification; must not block the shutdown.
        for admin_id in settings.get_admin_ids():
            try:
                await bot.send_message(
                    admin_id,
                    f"🔴 <b>{settings.bot_name}</b> is not running: no database connection.",
                )
            except Exception:
                pass
        # SystemExit (a BaseException) is guaranteed not to be swallowed by
        # `except Exception` handlers inside aiogram, so the process exits
        # with code 1.
        raise SystemExit(1) from e

    # Bot link in result messages: BOT_PUBLIC_USERNAME, or the bot's own username.
    if not formatter.BOT_LINK:
        me = await bot.get_me()
        formatter.set_bot_username(me.username or "")

    # Notify admins about a successful start (if ADMIN_IDS is set)
    for admin_id in settings.get_admin_ids():
        try:
            await bot.send_message(admin_id, f"✅ <b>{settings.bot_name}</b> started.")
        except Exception:
            pass


async def on_shutdown(bot: Bot) -> None:
    """Bot shutdown actions."""
    logger.info("Bot is stopping...")
    await close_db()

    for admin_id in settings.get_admin_ids():
        try:
            await bot.send_message(admin_id, f"🔴 <b>{settings.bot_name}</b> stopped.")
        except Exception:
            pass


async def main() -> None:
    """Main function: assemble and run the bot."""

    # Bot initialisation
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Dispatcher. MemoryStorage (in-memory FSM) is set EXPLICITLY: the content-filter
    # (18+ mode) state lives in per-user FSM state and persists between requests within
    # a session, BUT resets to the default (normal) on bot restart. That is a safe
    # default: 18+ mode never stays switched on "forever".
    dp = Dispatcher(storage=MemoryStorage())

    # Register middleware for all update types.
    # DbSessionMiddleware is the outer one: it creates the handler session (search) + commit.
    # UserStatsMiddleware records stats in its OWN short transaction: previously it wrote
    # into the handler session and held the user_stats row lock for the whole search
    # (causing cascading minute-long delays). Hence it takes a session factory, not
    # data['session'].
    dp.message.middleware(DbSessionMiddleware(AsyncSessionFactory))
    dp.message.middleware(UserStatsMiddleware(AsyncSessionFactory))
    dp.callback_query.middleware(DbSessionMiddleware(AsyncSessionFactory))
    dp.callback_query.middleware(UserStatsMiddleware(AsyncSessionFactory))

    # Include routers (order matters: start before search; feedback BEFORE search, so
    # the "📝 Feedback" button and the feedback text in FSM mode never reach search).
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(feedback.router)
    dp.include_router(search.router)

    # Startup/shutdown hooks
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Start polling
    try:
        logger.info("Polling started.")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=True,  # Skip updates accumulated while offline
        )
    finally:
        await bot.session.close()
        logger.info("Bot session closed.")


if __name__ == "__main__":
    asyncio.run(main())
