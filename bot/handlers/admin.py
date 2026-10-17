"""
TeleSeek admin handlers: analytics summary.

The /stats command is available ONLY to admins (by Telegram id from ADMIN_IDS in .env).
For non-admins the command is silently ignored (its existence is not revealed).
"""

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database import queries

logger = logging.getLogger(__name__)

router = Router(name="admin")


def _is_admin(user_id: int) -> bool:
    """True if user_id is in ADMIN_IDS."""
    return user_id in settings.get_admin_ids()


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    """
    /stats: analytics summary for an admin.

    Shows: total users, active in the last 7 days, total requests, total searches
    and how many of them returned nothing, top queries and top EMPTY queries (gaps
    in the index, i.e. what content to collect next).
    """
    if message.from_user is None or not _is_admin(message.from_user.id):
        # Silently ignore: do not reply to non-admins or reveal the command.
        return

    summary = await queries.get_analytics_summary(session)
    top = await queries.get_top_search_queries(session, limit=10)
    empty = await queries.get_top_search_queries(session, limit=10, empty_only=True)

    lines = [
        "📊 <b>TeleSeek — statistics</b>",
        "",
        "<b>Audience</b>",
        f"👤 Total users: <b>{summary['total_users']}</b>",
        f"🟢 Active in the last 7 days: <b>{summary['active_7d']}</b>",
        f"🔁 Total requests: <b>{summary['total_requests']}</b>",
        "",
        "<b>Search</b>",
        f"🔍 Total searches: <b>{summary['total_searches']}</b>",
        f"🕳 Of which with no results: <b>{summary['empty_searches']}</b>",
    ]

    lines.append("")
    lines.append("<b>🔝 Top queries</b>")
    if top:
        for i, (q, cnt) in enumerate(top, 1):
            lines.append(f"{i}. {q} — <i>{cnt}</i>")
    else:
        lines.append("<i>no data yet</i>")

    lines.append("")
    lines.append("<b>🕳 Top ZERO-RESULT queries</b> <i>(missing from the index)</i>")
    if empty:
        for i, (q, cnt) in enumerate(empty, 1):
            lines.append(f"{i}. {q} — <i>{cnt}</i>")
    else:
        lines.append("<i>no zero-result queries yet</i>")

    lines.append("")
    lines.append("<i>Counted since analytics was deployed (no retrospective data).</i>")

    await message.answer("\n".join(lines))
