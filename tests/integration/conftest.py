"""
Fixtures for integration tests.

Integration tests run against a real PostgreSQL database with the schema
applied (`alembic upgrade head`) — in the compose stack:

    docker compose --profile test run --rm tests

DATABASE_URL is read from the environment; without it the tests are skipped.
The bot settings are not imported, so no bot token is required. Every test uses
its own technical ids/usernames and removes what it creates.
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def session_factory():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set: integration tests need a database")
    engine = create_async_engine(url, pool_size=5, max_overflow=5)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def demo_data(session_factory):
    """Skip tests that rely on the synthetic demo data set if it is not loaded."""
    async with session_factory() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM channels WHERE username LIKE 'teleseek_demo_%'"))).scalar_one()
    if not n:
        pytest.skip("demo data is not loaded (run: python -m scripts.seed_data)")
    return session_factory
