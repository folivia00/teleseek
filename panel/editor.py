"""
Channel labelling editor (18+ flag / category) — backend of the panel's "Editor" tab.

Two-role architecture:
  * READ (list/search/categories/log) — via the read-only role teleseek_ro
    (the same engine used by all panel analytics). Changes nothing.
  * WRITE (editing is_adult/category + the edit log) — via the NARROW role teleseek_editor,
    which at the database GRANT level has UPDATE on exactly (is_adult, category) of channels
    and INSERT into channel_edit_log. It physically cannot touch parse_status,
    assigned_account, posts, etc. — even if the code tried to.

Edit safety:
  * the name of the edited field comes from a WHITELIST (is_adult|category), never from
    user input as a column name (no column-name SQL injection); values are bound parameters;
  * category is accepted only from the EXISTING set of categories (protects against typos
    and arbitrary values);
  * every edit → a row in channel_edit_log (before→after, timestamp) for audit/rollback;
  * edits apply to ONE channel at a time (no bulk operations).
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Only these fields are editable. The field name is NEVER taken from the request as a
# column name — it is selected by branching over this set (column-injection protection).
EDITABLE_FIELDS = ("is_adult", "category")

_PAGE_SIZE_MAX = 100


async def list_channels(
    ro_engine: AsyncEngine,
    *,
    q: Optional[str] = None,
    only_adult: bool = True,
    category: Optional[str] = None,
    has_posts: Optional[bool] = None,
    page: int = 0,
    page_size: int = 30,
) -> dict:
    """Channel list (read-only, paginated). only_adult=True → only is_adult=TRUE
    (focus: find mislabelled channels). q — search by username/title (ILIKE).
    has_posts: True=only with posts, False=only without posts, None=all.

    Keep postgres load low: the post count is a CORRELATED count only for the rows of
    the PAGE (30, index ix_posts_channel_id), NOT a full GROUP BY over all posts. The
    has/has-no posts filter uses EXISTS (index probe until the first match), not an aggregate."""
    page = max(0, int(page))
    page_size = min(_PAGE_SIZE_MAX, max(1, int(page_size)))
    offset = page * page_size

    where = ["1=1"]
    params: dict[str, Any] = {}
    if only_adult:
        where.append("c.is_adult IS TRUE")
    if category:
        where.append("c.category = :cat")
        params["cat"] = category
    if q:
        where.append("(c.username ILIKE :q OR c.title ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    if has_posts is True:
        where.append("EXISTS (SELECT 1 FROM posts p WHERE p.channel_id = c.id)")
    elif has_posts is False:
        where.append("NOT EXISTS (SELECT 1 FROM posts p WHERE p.channel_id = c.id)")
    wsql = " AND ".join(where)

    async with ro_engine.connect() as conn:
        total = (await conn.execute(
            text(f"SELECT count(*) FROM channels c WHERE {wsql}"), params)).scalar() or 0
        rows = (await conn.execute(text(
            f"""SELECT c.id, c.username, c.title, c.category, c.is_adult, c.members_count,
                       (SELECT count(*) FROM posts p WHERE p.channel_id = c.id) AS posts_count
                  FROM channels c WHERE {wsql}
                 ORDER BY c.members_count DESC NULLS LAST, c.id
                 LIMIT :lim OFFSET :off"""),
            {**params, "lim": page_size, "off": offset})).all()

        # parse_status (the "private/restricted" indicator) — in ONE query for all ids of
        # the page (channel_queue has no index on channel_id, but the table is small and
        # cached; a single pass instead of a per-row correlated subquery keeps postgres load low).
        ids = [r[0] for r in rows]
        pstatus: dict[int, str] = {}
        if ids:
            prows = (await conn.execute(text(
                "SELECT channel_id, parse_status::text FROM channel_queue "
                "WHERE channel_id = ANY(:ids)"), {"ids": ids})).all()
            pstatus = {pr[0]: pr[1] for pr in prows}

    items = [{
        "id": r[0], "username": r[1], "title": r[2], "category": r[3],
        "is_adult": r[4], "members_count": r[5] or 0,
        "posts_count": int(r[6] or 0), "parse_status": pstatus.get(r[0]),
    } for r in rows]
    return {"items": items, "total": int(total), "page": page,
            "page_size": page_size, "pages": (int(total) + page_size - 1) // page_size}


async def list_categories(ro_engine: AsyncEngine) -> list[str]:
    """Existing categories (for the dropdown) — from channel_queue (the CSV source) and
    channels, combined. Read-only."""
    async with ro_engine.connect() as conn:
        rows = (await conn.execute(text(
            """SELECT DISTINCT category FROM (
                   SELECT category FROM channel_queue WHERE category IS NOT NULL AND category <> ''
                   UNION
                   SELECT category FROM channels WHERE category IS NOT NULL AND category <> ''
               ) s ORDER BY category"""))).all()
    return [r[0] for r in rows]


async def recent_log(editor_engine: AsyncEngine, *, channel_id: Optional[int] = None,
                     limit: int = 20) -> list[dict]:
    """Most recent edits from the log (via the editor role, which has SELECT on the log)."""
    limit = min(100, max(1, int(limit)))
    sql = ("SELECT channel_id, username, field, old_value, new_value, created_at "
           "FROM channel_edit_log {where} ORDER BY id DESC LIMIT :lim")
    params: dict[str, Any] = {"lim": limit}
    where = ""
    if channel_id is not None:
        where = "WHERE channel_id = :cid"
        params["cid"] = int(channel_id)
    async with editor_engine.connect() as conn:
        rows = (await conn.execute(text(sql.format(where=where)), params)).all()
    return [{"channel_id": r[0], "username": r[1], "field": r[2],
             "old_value": r[3], "new_value": r[4],
             "created_at": r[5].isoformat() if r[5] is not None else None} for r in rows]


def _norm_value(field: str, raw: Any, valid_categories: list[str]) -> tuple[Any, str]:
    """Validate and normalize a field value. Returns (value_for_db, text_for_log).
    Raises ValueError on invalid input."""
    if field == "is_adult":
        if raw in (True, "true", "True", 1, "1"):
            return True, "true"
        if raw in (False, "false", "False", 0, "0"):
            return False, "false"
        if raw in (None, "null", "none", ""):
            return None, "null"          # "undefined" — resetting is allowed
        raise ValueError("is_adult: expected true/false/null")
    # category
    val = (str(raw).strip() if raw is not None else "")
    if not val:
        raise ValueError("category: empty value")
    if len(val) > 64:
        raise ValueError("category: too long (>64)")
    if val not in valid_categories:
        raise ValueError(f"category: '{val}' is not in the list of existing categories")
    return val, val


async def apply_edit(
    editor_engine: AsyncEngine,
    ro_engine: AsyncEngine,
    *,
    channel_id: int,
    field: str,
    value: Any,
) -> dict:
    """Apply an edit to ONE channel via the narrow role and write the log entry.
    Returns {ok, channel_id, field, old, new}. Raises ValueError on bad input."""
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"Field '{field}' is not editable (allowed: {EDITABLE_FIELDS})")
    channel_id = int(channel_id)

    valid_categories = await list_categories(ro_engine) if field == "category" else []
    db_value, new_text = _norm_value(field, value, valid_categories)

    # Current value (for the before→after log) — via the narrow role (it has SELECT on channels).
    async with editor_engine.connect() as conn:
        cur = (await conn.execute(
            text("SELECT username, is_adult, category FROM channels WHERE id = :id"),
            {"id": channel_id})).first()
    if cur is None:
        raise ValueError(f"Channel id={channel_id} not found")
    username = cur[0]
    old_text = ("true" if cur[1] is True else "false" if cur[1] is False else "null") \
        if field == "is_adult" else (cur[2] if cur[2] is not None else "")

    # WRITE: UPDATE of a strictly selected column (name from the whitelist, not from input)
    # + log entry, in ONE transaction of the narrow role. Rollback is handled by the context manager.
    async with editor_engine.begin() as conn:
        if field == "is_adult":
            await conn.execute(
                text("UPDATE channels SET is_adult = :v WHERE id = :id"),
                {"v": db_value, "id": channel_id})
        else:  # category
            await conn.execute(
                text("UPDATE channels SET category = :v WHERE id = :id"),
                {"v": db_value, "id": channel_id})
        await conn.execute(text(
            """INSERT INTO channel_edit_log (channel_id, username, field, old_value, new_value, source)
               VALUES (:cid, :un, :f, :ov, :nv, 'panel')"""),
            {"cid": channel_id, "un": username, "f": field,
             "ov": str(old_text), "nv": str(new_text)})

    return {"ok": True, "channel_id": channel_id, "field": field,
            "old": old_text, "new": new_text, "username": username}
