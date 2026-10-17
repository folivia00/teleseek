"""
PostgreSQL database connection.
Async engine + sessionmaker based on SQLAlchemy 2.x.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.config import settings

# Global async engine
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=False,          # True to debug SQL queries
    pool_size=10,        # Connection pool size
    max_overflow=20,     # Extra connections beyond the pool
    pool_pre_ping=True,  # Check the connection before use
)

# Session factory
AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Objects stay usable after commit
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager that provides a DB session.

    Usage:
        async with get_session() as session:
            result = await session.execute(...)
    """
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def close_db() -> None:
    """Close the connection pool when the bot shuts down."""
    await engine.dispose()
