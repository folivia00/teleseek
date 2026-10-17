"""
/start and /help command handlers for TeleSeek.

Texts are multilingual: fetched via i18n.t(key, lang). The language reaches handlers
as the ``lang`` kwarg from UserStatsMiddleware (auto-detected from Telegram
language_code, kept per user). Onboarding: a welcome WITHOUT clickable example buttons.
"""

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.i18n import t
from bot.keyboards.main_menu import build_main_reply_keyboard

router = Router(name="start")


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, lang: str = "en") -> None:
    """/start handler: welcome with a reply keyboard in the user's language.

    Resets the content filter to normal (safe default): after /start, 18+ content is
    not shown until the user explicitly flips the toggle.

    Onboarding: no clickable example buttons; the user types their own query right
    away. The menu emoji in the welcome text show what can be filtered/sorted.

    Also resets the FSM state (set_state(None)): /start leaves the "awaiting feedback"
    mode, so the next text goes to search again, not to feedback.
    """
    await state.set_state(None)
    await state.update_data(adult_only=False)
    await message.answer(
        t("welcome", lang),
        reply_markup=build_main_reply_keyboard(lang),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, lang: str = "en") -> None:
    """/help handler: help text in the user's language."""
    await message.answer(t("help", lang))


# The "🔍 Search" / "📖 Help" / "🌐 Language" reply buttons are handled by a SINGLE guard
# in search.handle_search_query (their text is NOT sent to search and is recognised in
# any language via i18n.all_texts), which is robust against stale keyboards and router
# order. The "🔥 Hot" button and /hot were removed entirely (privacy). Example buttons
# were removed as well.
