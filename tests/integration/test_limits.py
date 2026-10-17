"""
Limits infrastructure, without Telegram.

Main property: a LONG-RUNNING process (like the running bot) picks up a setting
changed in the database WITHOUT a restart — no later than the cache TTL.
Also: garbage/out-of-range values fall back to the defaults, premium
grant/expiry/revoke, and limits being disabled by default.
Uses a technical user id and restores every value it changes.
"""

import asyncio
import time

import pytest
from sqlalchemy import text

from bot import limits as bot_limits
from bot.database import queries

UID = -999_400_001


async def _set_daily(factory, value: str) -> None:
    async with factory() as w:
        await w.execute(
            text("UPDATE app_settings SET value = :v, updated_at = now(), "
                 "updated_by = 'test_limits' WHERE key = 'free_daily_searches'"),
            {"v": value})
        await w.commit()


async def _get_daily_raw(factory) -> str:
    async with factory() as s:
        return (await s.execute(text(
            "SELECT value FROM app_settings WHERE key = 'free_daily_searches'"))).scalar_one()


async def test_setting_change_is_picked_up_without_restart(session_factory) -> None:
    # Takes up to the cache TTL (about a minute): it waits for the real refresh.
    original = await _get_daily_raw(session_factory)
    try:
        async with session_factory() as s:
            start = await bot_limits.get_limits(s)
            target = 7 if start["daily_searches"] != 7 else 8
            # Simulate an edit from the panel (the panel does the same via the narrow role).
            await _set_daily(session_factory, str(target))

            t0 = time.time()
            picked = None
            while time.time() - t0 < bot_limits.TTL_SEC + 15:
                if (await bot_limits.get_limits(s))["daily_searches"] == target:
                    picked = time.time() - t0
                    break
                await asyncio.sleep(2)
        assert picked is not None, "the running process did not pick up the new value"
        assert picked <= bot_limits.TTL_SEC + 15
    finally:
        await _set_daily(session_factory, original)


@pytest.mark.parametrize("bad", ["0", "-5", "999999", "abc", ""])
async def test_garbage_values_fall_back_to_defaults(session_factory, bad: str) -> None:
    original = await _get_daily_raw(session_factory)
    try:
        await _set_daily(session_factory, bad)
        bot_limits._cache._at = 0.0          # reset the TTL to re-read immediately
        async with session_factory() as s:
            got = (await bot_limits.get_limits(s))["daily_searches"]
        assert got == int(bot_limits.DEFAULTS["free_daily_searches"])
    finally:
        await _set_daily(session_factory, original)
        bot_limits._cache._at = 0.0


async def test_premium_grant_expiry_and_revoke(session_factory) -> None:
    async with session_factory() as s:
        await s.execute(text("DELETE FROM user_subscription WHERE telegram_user_id = :u"), {"u": UID})
        await s.commit()
        try:
            assert await queries.is_premium(s, UID) is False

            await s.execute(text("""
                INSERT INTO user_subscription (telegram_user_id, tier, status, source, expires_at)
                VALUES (:u, 'premium', 'active', 'manual', now() + interval '30 days')
            """), {"u": UID})
            await s.commit()
            assert await queries.is_premium(s, UID) is True

            # An expired subscription is not active (the term is honoured).
            await s.execute(text(
                "UPDATE user_subscription SET expires_at = now() - interval '1 day' WHERE telegram_user_id = :u"),
                {"u": UID})
            await s.commit()
            assert await queries.is_premium(s, UID) is False

            # No expiry.
            await s.execute(text(
                "UPDATE user_subscription SET expires_at = NULL WHERE telegram_user_id = :u"), {"u": UID})
            await s.commit()
            assert await queries.is_premium(s, UID) is True

            # Revoke.
            await s.execute(text(
                "UPDATE user_subscription SET status = 'cancelled' WHERE telegram_user_id = :u"), {"u": UID})
            await s.commit()
            assert await queries.is_premium(s, UID) is False
        finally:
            await s.rollback()
            await s.execute(text("DELETE FROM user_subscription WHERE telegram_user_id = :u"), {"u": UID})
            await s.commit()


async def test_limits_disabled_by_default(session_factory) -> None:
    bot_limits._cache._at = 0.0
    async with session_factory() as s:
        assert (await bot_limits.get_limits(s))["enabled"] is False
