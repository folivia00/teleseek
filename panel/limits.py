"""
Panel tab "Limits & access".

What it provides:
  * reading/editing limit parameters (`app_settings`) with BOUNDS VALIDATION;
  * manual granting/revoking of premium (`user_subscription`) with an expiry;
  * an action log (`admin_action_log`) — who, what, before→after;
  * cheap usage metrics from telemetry (`interaction_log`).

Principles (same as the channel editor):
  * writes go ONLY through the narrow role `teleseek_editor`, which at the database GRANT
    level has column-level UPDATE on settings, INSERT/UPDATE on subscriptions and INSERT on
    the log — and nothing else;
  * every change → a log row in the SAME transaction (never diverges from the actual change);
  * value bounds are checked BEFORE writing; invalid input is rejected with a clear message.

Cheapness: every query here hits small tables or slices of `interaction_log` via the
(action, created_at) index. No count(*) over posts and no joins to content — heavy panel
aggregates evict the search cache. No heavy aggregates by design.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Bounds — the same as in the bot (bot/limits.py). Duplicated on purpose: the panel and
# the bot are deployed as separate images; a shared module would couple them as a dependency.
# Values are based on production usage data (p99 = 22 searches/day, max 46;
# MAX_RESULTS=300 → at most 30 meaningful pages).
BOUNDS: dict[str, tuple[int, int]] = {
    "free_daily_searches": (1, 1000),
    "free_max_pages": (1, 100),
}
BOOL_KEYS = ("limits_enabled",)
EDITABLE_KEYS = tuple(BOUNDS) + BOOL_KEYS

# Manual premium duration: 1 day … 5 years. Zero/negative is rejected (that is not "disable" —
# there is a separate action for that); unlimited is allowed via an explicit flag.
DAYS_BOUNDS = (1, 1825)


def _norm_setting(key: str, raw: Any) -> tuple[str, str]:
    """Validate and normalize a setting value → (value_for_db, text_for_log).

    Raises ValueError with a clear message — the panel shows it to the user and the
    product is left unchanged (bad input is simply not written)."""
    if key not in EDITABLE_KEYS:
        raise ValueError(f"Parameter '{key}' is not editable (allowed: {', '.join(EDITABLE_KEYS)})")

    if key in BOOL_KEYS:
        if raw in (True, "true", "True", 1, "1", "on", "yes"):
            return "true", "true"
        if raw in (False, "false", "False", 0, "0", "off", "no"):
            return "false", "false"
        raise ValueError(f"{key}: expected true/false")

    lo, hi = BOUNDS[key]
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{key}: an integer is required (got {raw!r})")
    if val < lo or val > hi:
        raise ValueError(f"{key}: allowed range is {lo} to {hi} (got {val})")
    return str(val), str(val)


async def get_state(ro_engine: AsyncEngine) -> dict:
    """Current settings + premium summary. Small tables only."""
    async with ro_engine.connect() as conn:
        settings = {str(k): {"value": str(v), "updated_at": u.isoformat() if u else None,
                             "updated_by": b}
                    for k, v, u, b in (await conn.execute(text(
                        "SELECT key, value, updated_at, updated_by FROM app_settings"))).all()}
        prem = (await conn.execute(text("""
            SELECT count(*) FILTER (WHERE status = 'active'
                                     AND (expires_at IS NULL OR expires_at > now())) AS active,
                   count(*) FILTER (WHERE status = 'active' AND expires_at IS NOT NULL
                                     AND expires_at <= now()) AS expired,
                   count(*) AS total
            FROM user_subscription
        """))).first()
    return {
        "settings": settings,
        "bounds": {k: {"min": v[0], "max": v[1]} for k, v in BOUNDS.items()},
        "premium": {"active": int(prem[0]) if prem else 0,
                    "expired": int(prem[1]) if prem else 0,
                    "total": int(prem[2]) if prem else 0},
    }


async def update_setting(
    editor_engine: AsyncEngine, *, key: str, value: Any, actor: str = "panel"
) -> dict:
    """Change a parameter via the narrow role + log entry (single transaction)."""
    db_value, new_text = _norm_setting(key, value)

    async with editor_engine.begin() as conn:
        cur = (await conn.execute(
            text("SELECT value FROM app_settings WHERE key = :k"), {"k": key})).first()
        if cur is None:
            # Keys are created by migrations, not by the panel — otherwise the parameter set would sprawl.
            raise ValueError(f"Parameter '{key}' not found in app_settings")
        old_text = str(cur[0])
        if old_text == db_value:
            return {"ok": True, "key": key, "old": old_text, "new": db_value, "changed": False}

        await conn.execute(text("""
            UPDATE app_settings SET value = :v, updated_at = now(), updated_by = :by
            WHERE key = :k
        """), {"v": db_value, "by": actor[:64], "k": key})
        await conn.execute(text("""
            INSERT INTO admin_action_log (actor, action, target, old_value, new_value)
            VALUES (:actor, 'setting_update', :target, :ov, :nv)
        """), {"actor": actor[:64], "target": key[:64], "ov": old_text[:128], "nv": new_text[:128]})

    return {"ok": True, "key": key, "old": old_text, "new": new_text, "changed": True}


async def grant_premium(
    editor_engine: AsyncEngine,
    *,
    telegram_user_id: int,
    days: Optional[int] = None,
    forever: bool = False,
    actor: str = "panel",
) -> dict:
    """Grant premium manually (until payments exist): testing, early users, compensation.

    Stored as a subscription row with `source='manual'`. The same model serves both manual
    grants and future Stars payments — those will add `source='stars'`, `external_id`
    (payment id) and `is_recurring=true`, while the bot's access check stays unchanged.
    """
    uid = int(telegram_user_id)
    if forever:
        expires_sql = "NULL"
        params: dict[str, Any] = {"uid": uid}
        human = "no expiry"
    else:
        lo, hi = DAYS_BOUNDS
        if days is None:
            raise ValueError("Specify a duration in days or check \"no expiry\"")
        try:
            d = int(days)
        except (TypeError, ValueError):
            raise ValueError(f"Duration: an integer number of days is required (got {days!r})")
        if d < lo or d > hi:
            raise ValueError(f"Duration: allowed range is {lo} to {hi} days (got {d})")
        expires_sql = "now() + make_interval(days => :days)"
        params = {"uid": uid, "days": d}
        human = f"{d} days"

    async with editor_engine.begin() as conn:
        # Cancel this user's active rows so parallel subscriptions do not pile up
        # (DELETE is intentionally not granted to the role — history is preserved).
        await conn.execute(text("""
            UPDATE user_subscription SET status = 'cancelled', updated_at = now()
            WHERE telegram_user_id = :uid AND status = 'active'
        """), {"uid": uid})
        row = (await conn.execute(text(f"""
            INSERT INTO user_subscription
                (telegram_user_id, tier, status, source, starts_at, expires_at)
            VALUES (:uid, 'premium', 'active', 'manual', now(), {expires_sql})
            RETURNING id, expires_at
        """), params)).first()
        await conn.execute(text("""
            INSERT INTO admin_action_log (actor, action, target, old_value, new_value)
            VALUES (:actor, 'premium_grant', :target, NULL, :nv)
        """), {"actor": actor[:64], "target": str(uid)[:64], "nv": human[:128]})

    return {"ok": True, "telegram_user_id": uid, "subscription_id": int(row[0]),
            "expires_at": row[1].isoformat() if row[1] is not None else None, "human": human}


async def revoke_premium(
    editor_engine: AsyncEngine, *, telegram_user_id: int, actor: str = "panel"
) -> dict:
    """Revoke premium (all of the user's active rows → cancelled)."""
    uid = int(telegram_user_id)
    async with editor_engine.begin() as conn:
        res = await conn.execute(text("""
            UPDATE user_subscription SET status = 'cancelled', updated_at = now()
            WHERE telegram_user_id = :uid AND status = 'active'
        """), {"uid": uid})
        n = res.rowcount or 0
        if n:
            await conn.execute(text("""
                INSERT INTO admin_action_log (actor, action, target, old_value, new_value)
                VALUES (:actor, 'premium_revoke', :target, 'active', 'cancelled')
            """), {"actor": actor[:64], "target": str(uid)[:64]})
    return {"ok": True, "telegram_user_id": uid, "revoked": n}


async def find_user(ro_engine: AsyncEngine, telegram_user_id: int) -> dict:
    """User card: activity (user_stats) + current subscription status."""
    uid = int(telegram_user_id)
    async with ro_engine.connect() as conn:
        u = (await conn.execute(text("""
            SELECT username, requests_count, first_seen, last_seen, language
            FROM user_stats WHERE telegram_user_id = :uid
        """), {"uid": uid})).first()
        sub = (await conn.execute(text("""
            SELECT id, tier, status, source, starts_at, expires_at, is_recurring
            FROM user_subscription
            WHERE telegram_user_id = :uid
            ORDER BY id DESC LIMIT 1
        """), {"uid": uid})).first()
        searches = (await conn.execute(text(
            "SELECT count(*) FROM search_log WHERE telegram_user_id = :uid"), {"uid": uid}
        )).scalar_one()

    now = datetime.now(timezone.utc)
    active = bool(sub and sub[2] == "active" and (sub[5] is None or sub[5] > now))
    return {
        "found": u is not None or sub is not None,
        "telegram_user_id": uid,
        "user": None if u is None else {
            "username": u[0], "requests": int(u[1]),
            "first_seen": u[2].isoformat() if u[2] else None,
            "last_seen": u[3].isoformat() if u[3] else None,
            "language": u[4], "searches": int(searches),
        },
        "premium_active": active,
        "subscription": None if sub is None else {
            "id": int(sub[0]), "tier": sub[1], "status": sub[2], "source": sub[3],
            "starts_at": sub[4].isoformat() if sub[4] else None,
            "expires_at": sub[5].isoformat() if sub[5] else None,
            "is_recurring": bool(sub[6]),
        },
    }


async def recent_log(ro_engine: AsyncEngine, limit: int = 20) -> list[dict]:
    """Most recent panel actions (log). Small table + index on created_at."""
    lim = max(1, min(int(limit), 200))
    async with ro_engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT actor, action, target, old_value, new_value, created_at
            FROM admin_action_log ORDER BY id DESC LIMIT :lim
        """), {"lim": lim})).all()
    return [{"actor": r[0], "action": r[1], "target": r[2], "old": r[3], "new": r[4],
             "at": r[5].isoformat() if r[5] else None} for r in rows]


async def usage_stats(ro_engine: AsyncEngine, days: int = 14) -> dict:
    """Cheap usage metrics from telemetry.

    All slices use the (action, created_at) index over a small window and do NOT touch
    posts/media — heavy panel aggregates used to evict the search cache.
    The limit_hit/upsell fields are prepared in advance and will fill once limits are enabled.
    """
    d = max(1, min(int(days), 90))
    async with ro_engine.connect() as conn:
        depth = [{"page": int(p), "hits": int(c)} for p, c in (await conn.execute(text("""
            SELECT page, count(*) FROM interaction_log
            WHERE action = 'page' AND page IS NOT NULL
              AND created_at >= now() - make_interval(days => :d)
            GROUP BY page ORDER BY page LIMIT 30
        """), {"d": d})).all()]
        filters = [{"content_type": ct or "?", "hits": int(c)} for ct, c in (await conn.execute(text("""
            SELECT content_type, count(*) FROM interaction_log
            WHERE action = 'filter' AND created_at >= now() - make_interval(days => :d)
            GROUP BY content_type ORDER BY 2 DESC LIMIT 10
        """), {"d": d})).all()]
        actions = [{"action": a, "hits": int(c), "users": int(u)} for a, c, u in (await conn.execute(text("""
            SELECT action, count(*), count(DISTINCT telegram_user_id) FROM interaction_log
            WHERE created_at >= now() - make_interval(days => :d)
            GROUP BY action ORDER BY 2 DESC
        """), {"d": d})).all()]
        reached_end = (await conn.execute(text("""
            SELECT count(*) FILTER (WHERE is_last_page), count(*)
            FROM interaction_log
            WHERE action = 'page' AND created_at >= now() - make_interval(days => :d)
        """), {"d": d})).first()
    return {
        "days": d, "depth": depth, "filters": filters, "actions": actions,
        "reached_end": {"yes": int(reached_end[0]) if reached_end else 0,
                        "total": int(reached_end[1]) if reached_end else 0},
    }
