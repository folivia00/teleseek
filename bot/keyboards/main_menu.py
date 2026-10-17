"""
TeleSeek main menu.
Reply keyboard with the main actions (shown at the bottom of the screen).
"""

from aiogram.types import (
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from bot.i18n import t


def build_main_reply_keyboard(lang: str = "en") -> ReplyKeyboardMarkup:
    """
    Main-menu reply keyboard (shown at the bottom after /start).

    Buttons are in the user's language: 🔍 Search · 📖 Help · 🌐 Language, with
    📝 Feedback on a second row (always at hand without crowding the main search
    row). Emoji are universal; only the labels are translated.
    "🌐 Language" switches the UI language (handled in search.handle_search_query),
    "📝 Feedback" is handled in handlers/feedback.py; both are recognised by text
    in any language via i18n.all_texts.

    The "Hot" block was removed earlier (privacy).
    """
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text=t("btn_search", lang)),
        KeyboardButton(text=t("btn_help", lang)),
        KeyboardButton(text=t("btn_language", lang)),
    )
    builder.row(KeyboardButton(text=t("btn_feedback", lang)))
    return builder.as_markup(
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder=t("input_placeholder", lang),
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    """Remove the reply keyboard."""
    return ReplyKeyboardRemove()
