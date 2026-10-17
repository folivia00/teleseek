"""
User accounting middleware (audience analytics).

Counts EVERY user interaction (any message/callback): an atomic upsert into
user_stats (new -> INSERT, existing -> last_seen + requests_count += 1).
Minimal personal data: only the Telegram id + username (for reference) + activity.

🔴 IMPORTANT: accounting runs in its OWN SHORT transaction, NOT in the handler session.
It used to be the other way round: the upsert ran on the shared session BEFORE the
handler, so the `user_stats` row lock was held for the whole processing (search
included). Whenever a search fell into a minute-long ILIKE scan, the lock was held for
minutes: the next update from the SAME user waited for the previous one (INSERT into
user_stats averaged 22.8s with a 27-minute maximum on a 1.2k-row table), and
connections piled up until the SQLAlchemy pool was exhausted. Now the accounting
transaction is opened, committed and closed BEFORE the handler: the lock lives for
milliseconds, so a cascade is impossible by construction.

Accounting data is not lost either: its own commit does not depend on the handler
outcome (previously a handler failure also rolled back the telemetry; now it is
already committed).

An accounting error must not break update processing; it is logged and swallowed.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import i18n
from bot.database import queries

logger = logging.getLogger(__name__)


class UserStatsMiddleware(BaseMiddleware):
    """
    Records each user update in user_stats + resolves the UI language.

    A single upsert does both: counts the interaction and returns the user's effective
    language (auto-detected from Telegram language_code on first contact, then kept
    per user). The language is put into ``data['lang']``; handlers receive it as the
    ``lang`` kwarg (aiogram passes keys whose names match).

    The session is its OWN: a factory is passed to the constructor, and the accounting
    transaction lives only for the upsert and is closed before the handler is called
    (see the module docstring).
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """
        Args:
            session_factory: Async session factory for the OWN short accounting
                transaction (not the handler session).
        """
        self._session_factory = session_factory
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Message and CallbackQuery both have from_user directly; take it from the
        # event without relying on the order of aiogram's built-in middleware
        # (event_from_user).
        user = getattr(event, "from_user", None)

        # Default language in case there is no user or the DB fails (the bot keeps going).
        lang = i18n.DEFAULT_LANG

        if user is not None and not getattr(user, "is_bot", False):
            detected = i18n.detect_lang(getattr(user, "language_code", None))
            try:
                # Short independent transaction: open -> upsert -> commit -> close.
                # The connection returns to the pool BEFORE search starts (no pile-up).
                async with self._session_factory() as session:
                    stored = await queries.upsert_user_stats(
                        session, user.id, user.username, detected
                    )
                    await session.commit()
                lang = i18n.normalize_lang(stored or detected)
            except Exception as e:
                # Stats/language are not critical; do not abort update processing.
                logger.warning("User accounting failed for %s: %s", getattr(user, "id", "?"), e)
                lang = i18n.normalize_lang(detected)

        data["lang"] = lang
        return await handler(event, data)
