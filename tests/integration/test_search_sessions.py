"""
Search sessions: only the latest result message of a user is active; stale or
forged identifiers are rejected; the cleanup keeps the latest session per user
and never touches analytics tables. Uses technical user ids and cleans up.
"""

from sqlalchemy import text

from bot.database import queries

UID_A = -999_100_001          # "user A"
UID_B = -999_100_002          # "user B" (isolation between users)
CHAT = -999_100_999

CLEANUP_SQL = """
    WITH ranked AS (
        SELECT id, row_number() OVER (
            PARTITION BY telegram_user_id ORDER BY created_at DESC, id DESC
        ) AS rn
        FROM search_session WHERE telegram_user_id IN (:a, :b)
    ), del AS (
        DELETE FROM search_session s USING ranked r
        WHERE s.id = r.id AND r.rn > 1 RETURNING s.id
    )
    SELECT count(*) FROM del
"""


async def _wipe(s) -> None:
    await s.execute(text("DELETE FROM search_session WHERE telegram_user_id IN (:a, :b)"),
                    {"a": UID_A, "b": UID_B})
    await s.commit()


async def test_search_sessions(session_factory) -> None:
    async with session_factory() as s:
        await _wipe(s)
        try:
            # 1) Query 1 → query 2: only the second result message stays active.
            await queries.start_search_session(s, UID_A, CHAT, 1001)
            await s.commit()
            assert await queries.is_active_session(s, UID_A, CHAT, 1001) is True
            await queries.start_search_session(s, UID_A, CHAT, 1002)
            await s.commit()
            assert await queries.is_active_session(s, UID_A, CHAT, 1001) is False
            assert await queries.is_active_session(s, UID_A, CHAT, 1002) is True

            # 2) Stale or forged identifiers.
            assert await queries.is_active_session(s, UID_A, CHAT, 999_999) is False   # never existed
            assert await queries.is_active_session(s, UID_A, CHAT - 1, 1002) is False  # another chat
            assert await queries.is_active_session(s, UID_B, CHAT, 1002) is False      # another user
            assert await queries.is_active_session(s, UID_B, CHAT, 5000) is False      # no sessions

            # 3) Registering the same message twice is idempotent.
            await queries.start_search_session(s, UID_A, CHAT, 1002)
            await s.commit()
            n = (await s.execute(text("SELECT count(*) FROM search_session WHERE telegram_user_id=:u"),
                                 {"u": UID_A})).scalar_one()
            assert n == 2
            assert await queries.is_active_session(s, UID_A, CHAT, 1002) is True

            # 4) Cleanup keeps the latest session per user and leaves analytics alone.
            for msg in (2001, 2002, 2003):
                await queries.start_search_session(s, UID_B, CHAT, msg)
            await s.commit()
            log_before = (await s.execute(text("SELECT count(*) FROM search_log"))).scalar_one()
            users_before = (await s.execute(text("SELECT count(*) FROM user_stats"))).scalar_one()

            deleted = (await s.execute(text(CLEANUP_SQL), {"a": UID_A, "b": UID_B})).scalar_one()
            await s.commit()
            assert deleted == 3                     # A: 1 extra row, B: 2 extra rows
            assert await queries.is_active_session(s, UID_A, CHAT, 1002) is True
            assert await queries.is_active_session(s, UID_B, CHAT, 2003) is True
            assert (await s.execute(text("SELECT count(*) FROM search_log"))).scalar_one() == log_before
            assert (await s.execute(text("SELECT count(*) FROM user_stats"))).scalar_one() == users_before
        finally:
            await s.rollback()
            await _wipe(s)
