"""
TeleSeek "Feedback" handlers.

Flow: "📝 Feedback" reply button (main menu) -> invitation to write a message
(FSM waiting state) -> the user's text is saved to the ``feedback`` table ->
a warm confirmation. Texts are multilingual (i18n, RU/EN by the user's language).

Anti-spam: strictly 1 message per hour per user (checked against the DB, so it
survives a bot restart, unlike MemoryStorage). Checked BOTH when the button is pressed
(so the user does not write in vain) AND before saving (guards against races/stale state).

Anonymity: the user id is NEVER shown to anyone in the bot; telegram_user_id is stored
only for internal moderation (internal admin panel) and rate limiting.

IMPORTANT (router order, main.py): the feedback router is included AFTER start/admin
(they handle commands) but BEFORE search; otherwise the button/feedback text would end
up in search. /start resets the waiting state (see start.cmd_start).
"""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import i18n
from bot.database import queries
from bot.i18n import t
from bot.keyboards.main_menu import build_main_reply_keyboard
from bot.safety import is_blocked_query

logger = logging.getLogger(__name__)

router = Router(name="feedback")

# Rate limit: 1 message per hour (anti-spam).
FEEDBACK_COOLDOWN_SEC = 3600
# Max feedback length (a Telegram message is <=4096; we cap it reasonably shorter).
FEEDBACK_MAX_LEN = 2000

# The button label is recognised in ANY language (guards against stale reply
# keyboards of users who last ran /start long ago).
_BTN_FEEDBACK = i18n.all_texts("btn_feedback")
# Other menu buttons: pressing one while awaiting feedback cancels that mode and the
# button performs its usual function (delegated to the search handler).
_OTHER_MENU = (
    i18n.all_texts("btn_search") | {"🔍 Поиск"}
    | i18n.all_texts("btn_help") | {"📖 Помощь"}
    | i18n.all_texts("btn_language")
)


class FeedbackStates(StatesGroup):
    """Feedback FSM: waiting for the feedback text after the button is pressed."""

    waiting_text = State()


async def _is_rate_limited(session: AsyncSession, user_id: int) -> bool:
    """True if less than an hour has passed since the user's last feedback."""
    elapsed = await queries.seconds_since_last_feedback(session, user_id)
    return elapsed is not None and elapsed < FEEDBACK_COOLDOWN_SEC


@router.message(F.text.in_(_BTN_FEEDBACK))
async def handle_feedback_button(
    message: Message, session: AsyncSession, state: FSMContext, lang: str = "en"
) -> None:
    """
    "📝 Feedback" pressed (in any state; pressing again just re-shows the
    invitation). If the rate limit is active, politely refuse right away instead
    of making the user write text in vain.
    """
    if message.from_user is not None and await _is_rate_limited(session, message.from_user.id):
        await message.answer(t("feedback_rate_limited", lang))
        return
    await state.set_state(FeedbackStates.waiting_text)
    await message.answer(t("feedback_prompt", lang))


@router.message(FeedbackStates.waiting_text, F.text)
async def handle_feedback_text(
    message: Message, session: AsyncSession, state: FSMContext, lang: str = "en"
) -> None:
    """
    Text while awaiting feedback -> save it.

    Menu buttons (Search/Help/Language) cancel the mode and perform their function,
    so the user is never "locked" in feedback mode. set_state(None) (NOT clear) keeps
    the FSM data (18+ mode).
    """
    raw = (message.text or "").strip()

    if raw in _OTHER_MENU:
        await state.set_state(None)
        # Delegate to the regular menu/search handler (it recognises and runs the button).
        from bot.handlers.search import handle_search_query
        await handle_search_query(message, session, state, lang)
        return

    if not raw:
        await message.answer(t("feedback_empty", lang))
        return
    if len(raw) > FEEDBACK_MAX_LEN:
        await message.answer(t("feedback_too_long", lang))
        return

    # Blocklist: feedback that matches the blocklist is NOT saved; the reply is the same
    # neutral one as in search, and the text is written nowhere (neither DB nor logs).
    # Checked BEFORE the rate limit: a blocked attempt does NOT consume the 1/hour
    # quota (the quota is based on actual feedback rows, and none is written).
    # Otherwise abuse would block a later legitimate message, and the refusal itself
    # would reveal that the attempt was "counted".
    if is_blocked_query(raw):
        await state.set_state(None)
        await message.answer(t("query_blocked", lang))
        return

    user_id = message.from_user.id if message.from_user else None

    # Re-check the limit before writing (the state may outlive a dialogue started more
    # than an hour ago; guards against races and bypass via two open chats).
    if user_id is not None and await _is_rate_limited(session, user_id):
        await state.set_state(None)
        await message.answer(t("feedback_rate_limited", lang))
        return

    await queries.add_feedback(session, user_id, raw, language=lang)
    await state.set_state(None)
    logger.info("Feedback: message received (%d chars)", len(raw))
    await message.answer(
        t("feedback_thanks", lang),
        reply_markup=build_main_reply_keyboard(lang),
    )


@router.message(FeedbackStates.waiting_text)
async def handle_feedback_non_text(message: Message, lang: str = "en") -> None:
    """Non-text (photo/sticker/voice) while waiting: ask for text, keep the mode."""
    await message.answer(t("feedback_empty", lang))
