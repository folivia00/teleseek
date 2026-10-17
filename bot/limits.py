"""
Usage-limit settings: read from the DB with a short-lived cache.

Why the DB rather than `.env`: a change made in the admin panel must apply WITHOUT
restarting the bot. A value in `.env` requires recreating the bot container, i.e.
downtime and a developer in the loop, which is unacceptable for a product knob.

Why a TTL cache rather than a read per request: settings change once in a few weeks,
but would otherwise be read on every user action. A 60-second TTL is the compromise:
a change propagates within a minute (effectively "instantly" for a human), while the
DB load stays at one tiny SELECT per minute (a three-row table).

Fault tolerance: if the DB is unavailable, the last known values are returned, and
if there are none, safe defaults with limits **disabled**. A failure to read settings
must neither break search nor accidentally restrict users.

⚠️ This module ONLY reads and returns values. Enforcing the limits (refusing a search,
upsell) is out of scope here on purpose; the master switch is off by default.
"""

import logging
import time
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.database import queries

logger = logging.getLogger(__name__)

# How many seconds to trust the values read.
TTL_SEC = 60.0

# Safe defaults: limits are DISABLED. They match the values seeded by the baseline migration,
# so behaviour does not depend on whether the DB has been read yet.
DEFAULTS: dict[str, str] = {
    "limits_enabled": "false",
    "free_daily_searches": "10",
    "free_max_pages": "3",
}

# ── Value bounds (shared by the bot and the admin panel) ─────────────────────
# Upper/lower bounds are based on production usage data, not guesswork:
#   * searches/day: p99 = 22, all-time maximum = 46 -> 1..1000 covers any sensible
#     policy with a huge margin; 0 is forbidden (it would mean "no search for anyone");
#   * pages: MAX_RESULTS=300 at 10 per page -> there is physically no page beyond 30;
#     1..100 leaves headroom for a larger MAX_RESULTS.
BOUNDS: dict[str, tuple[int, int]] = {
    "free_daily_searches": (1, 1000),
    "free_max_pages": (1, 100),
}
BOOL_KEYS = ("limits_enabled",)


class _SettingsCache:
    """TTL cache for settings. Never raises to the caller; only logs."""

    def __init__(self) -> None:
        self._values: dict[str, str] = dict(DEFAULTS)
        self._at: float = 0.0
        self._loaded_ok = False

    async def get(self, session: AsyncSession) -> dict[str, str]:
        now = time.time()
        if self._loaded_ok and (now - self._at) < TTL_SEC:
            return self._values
        try:
            fresh = await queries.load_settings(session)
            if fresh:
                merged = dict(DEFAULTS)
                merged.update(fresh)
                self._values = merged
                self._loaded_ok = True
            self._at = now
        except Exception as e:
            # Keep the previous values; on the very first failure, defaults (limits off).
            logger.warning("Failed to read app_settings: %s (keeping previous values)", e)
            self._at = now
        return self._values


_cache = _SettingsCache()


def _as_bool(raw: str) -> bool:
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _as_int(raw: str, key: str) -> int:
    """Integer within BOUNDS. Garbage/out-of-range -> default (never break the product)."""
    lo, hi = BOUNDS[key]
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("app_settings[%s]=%r is not a number, using default", key, raw)
        return int(DEFAULTS[key])
    if not (lo <= val <= hi):
        logger.warning("app_settings[%s]=%s out of bounds %s..%s, using default", key, val, lo, hi)
        return int(DEFAULTS[key])
    return val


async def get_limits(session: AsyncSession) -> dict:
    """Current limits: {'enabled': bool, 'daily_searches': int, 'max_pages': int}."""
    raw = await _cache.get(session)
    return {
        "enabled": _as_bool(raw.get("limits_enabled", DEFAULTS["limits_enabled"])),
        "daily_searches": _as_int(
            raw.get("free_daily_searches", DEFAULTS["free_daily_searches"]), "free_daily_searches"
        ),
        "max_pages": _as_int(
            raw.get("free_max_pages", DEFAULTS["free_max_pages"]), "free_max_pages"
        ),
    }


async def write_interaction(
    session_factory: async_sessionmaker[AsyncSession],
    telegram_user_id: Optional[int],
    action: str,
    **fields,
) -> None:
    """Write telemetry in its OWN short transaction, outside the search transaction.

    Called AFTER the reply has been sent, so it does not affect response latency.
    Any error is swallowed: telemetry must never break the product.
    """
    if telegram_user_id is None:
        return
    try:
        async with session_factory() as s:
            await queries.log_interaction(s, telegram_user_id, action, **fields)
            await s.commit()
    except Exception as e:
        logger.warning("Interaction telemetry not written (%s): %s", action, e)
