"""
Panel "Feedback" section — backend of the tab.

Two-role architecture (same as the editor):
  * READ (the feedback feed) — via the read-only role teleseek_ro (the panel's shared engine).
  * ACTIONS (delete / "favorite" toggle) — via the NARROW role teleseek_editor,
    which on feedback has exactly SELECT + UPDATE (is_favorite) + DELETE
    (grants — the baseline migration / scripts/setup_roles.py). The message text and author
    physically cannot be edited — column-level GRANT.

Load: the feed uses LIMIT/pagination — Index Scan Backward on ix_feedback_created_at,
no full scan; the table is small by nature (written by humans, rate-limited to 1/hour).
The user id is shown ONLY here (internal moderation, localhost-only panel) — it is never
shown to users in the bot.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_PAGE_SIZE_MAX = 100


async def list_feedback(
    ro_engine: AsyncEngine,
    *,
    only_favorites: bool = False,
    page: int = 0,
    page_size: int = 30,
) -> dict:
    """Feedback feed (read-only): newest first, paginated, optional "favorites only" filter."""
    page = max(0, int(page))
    page_size = min(_PAGE_SIZE_MAX, max(1, int(page_size)))
    offset = page * page_size

    where = "WHERE is_favorite IS TRUE" if only_favorites else ""

    async with ro_engine.connect() as conn:
        total = (await conn.execute(
            text(f"SELECT count(*) FROM feedback {where}"))).scalar() or 0
        rows = (await conn.execute(text(
            f"""SELECT id, telegram_user_id, text, language, is_favorite, created_at
                  FROM feedback {where}
                 ORDER BY created_at DESC, id DESC
                 LIMIT :lim OFFSET :off"""),
            {"lim": page_size, "off": offset})).all()

    items = [{
        "id": r[0],
        "user_id": r[1],                 # internal moderation; not shown in the bot
        "text": r[2],
        "language": r[3],
        "is_favorite": bool(r[4]),
        "created_at": r[5].isoformat() if r[5] is not None else None,
    } for r in rows]
    return {"items": items, "total": int(total), "page": page,
            "page_size": page_size, "pages": (int(total) + page_size - 1) // page_size}


async def set_favorite(editor_engine: AsyncEngine, *, feedback_id: int, value: bool) -> dict:
    """Toggle "favorite" on a single message (narrow role: UPDATE of is_favorite only)."""
    feedback_id = int(feedback_id)
    value = bool(value)
    async with editor_engine.begin() as conn:
        row = (await conn.execute(
            text("UPDATE feedback SET is_favorite = :v WHERE id = :id RETURNING id"),
            {"v": value, "id": feedback_id})).first()
    if row is None:
        raise ValueError(f"Feedback id={feedback_id} not found")
    return {"id": feedback_id, "is_favorite": value}


async def delete_feedback(editor_engine: AsyncEngine, *, feedback_id: int) -> dict:
    """Delete a single message (narrow role: DELETE on feedback). No bulk operations."""
    feedback_id = int(feedback_id)
    async with editor_engine.begin() as conn:
        row = (await conn.execute(
            text("DELETE FROM feedback WHERE id = :id RETURNING id"),
            {"id": feedback_id})).first()
    if row is None:
        raise ValueError(f"Feedback id={feedback_id} not found")
    return {"id": feedback_id, "deleted": True}
