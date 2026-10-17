"""
Lock cascade on user_stats: the analytics middleware writes in its OWN short
transaction, so a slow search no longer holds the user's row lock.

Scenario (technical user id, cleaned up afterwards):
  OLD: session A = upsert + a long query in ONE transaction → session B upserts
       the same user and MEASURES how long it waited;
  NEW: session A = upsert in its own short transaction (commit), then the long
       query → session B measures the same wait.
Expected: OLD ≈ the duration of the "search", NEW ≈ milliseconds.
"""

import asyncio
import time

from sqlalchemy import text

from bot.database import queries

TEST_UID = -999_000_001
SLOW_SEC = 3          # simulated "long search" inside a transaction
NAME = "lock_probe"


async def _slow(session, seconds: int) -> None:
    await session.execute(text("SELECT pg_sleep(:s)"), {"s": seconds})


async def _waiter(factory, started: asyncio.Event) -> float:
    """Session B: waits for the signal and measures how long its own upsert took."""
    await started.wait()
    await asyncio.sleep(0.3)          # make sure A really holds the row
    async with factory() as s:
        t0 = time.perf_counter()
        await queries.upsert_user_stats(s, TEST_UID, NAME, "en")
        await s.commit()
        return (time.perf_counter() - t0) * 1000


async def _scenario_old(factory) -> float:
    """upsert AND the long query in ONE transaction (the previous design)."""
    started = asyncio.Event()
    waiter = asyncio.create_task(_waiter(factory, started))
    async with factory() as s:
        await queries.upsert_user_stats(s, TEST_UID, NAME, "en")   # lock taken
        started.set()
        await _slow(s, SLOW_SEC)                                   # lock HELD
        await s.commit()                                           # released only now
    return await waiter


async def _scenario_new(factory) -> float:
    """upsert in its OWN short transaction, then the long query (the current design)."""
    started = asyncio.Event()
    waiter = asyncio.create_task(_waiter(factory, started))
    async with factory() as s:                    # short accounting transaction
        await queries.upsert_user_stats(s, TEST_UID, NAME, "en")
        await s.commit()                          # lock released IMMEDIATELY
    started.set()
    async with factory() as s:                    # "search" — no row lock any more
        await _slow(s, SLOW_SEC)
    return await waiter


async def test_short_accounting_transaction_removes_the_lock_cascade(session_factory) -> None:
    try:
        old_ms = await _scenario_old(session_factory)
        new_ms = await _scenario_new(session_factory)
        print(f"OLD wait: {old_ms:.0f} ms, NEW wait: {new_ms:.0f} ms")
        assert old_ms >= (SLOW_SEC - 1) * 1000     # the old design waits for the "search"
        assert new_ms < 500                        # the new one does not
    finally:
        async with session_factory() as s:
            await s.execute(text("DELETE FROM user_stats WHERE telegram_user_id = :u"), {"u": TEST_UID})
            await s.commit()
