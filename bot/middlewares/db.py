"""
Middleware that injects a database session into handlers.
Each update gets its own session with automatic commit/rollback.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DbSessionMiddleware(BaseMiddleware):
    """
    Middleware: creates a DB session for every incoming update.
    The session is available in handlers as data['session'].
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """
        Args:
            session_factory: SQLAlchemy async session factory.
        """
        self._session_factory = session_factory
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Create a session, pass it to the handler, close it when done."""
        async with self._session_factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise
