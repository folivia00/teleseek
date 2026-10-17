"""
Inline keyboard of content-type filters.
Bottom icon row: 🌐 📢 👥 💬 🖼 🎧 🎬 📁
"""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.services.search_engine import ContentType
from bot.utils.helpers import safe_callback

# Each filter: (emoji, English label, ContentType).
# VIDEOS come before posts (video is the more popular content type). Order:
# All · Channels · Groups · Videos · Posts · Photos · Audio · Files.
FILTERS = [
    ("🌐", "All",      ContentType.ALL),       # filter reset goes first
    ("📢", "Channels", ContentType.CHANNELS),
    ("👥", "Groups",   ContentType.GROUPS),
    ("🎬", "Videos",   ContentType.VIDEOS),    # videos before posts
    ("💬", "Posts",    ContentType.POSTS),
    ("🖼", "Photos",   ContentType.PHOTOS),
    ("🎧", "Audio",    ContentType.AUDIO),
    ("📁", "Files",    ContentType.DOCUMENTS),
]


def build_filter_keyboard(query: str, active: ContentType = ContentType.ALL) -> InlineKeyboardMarkup:
    """
    Build the inline filter keyboard shown under search results.
    The active filter is highlighted with a ✓ mark.

    Args:
        query: Current search query (embedded into callback_data).
        active: Active filter type, highlighted visually.

    Returns:
        InlineKeyboardMarkup with a single row of 8 buttons.
    """
    builder = InlineKeyboardBuilder()

    for icon, label, content_type in FILTERS:
        # Highlight the active filter
        btn_text = f"[{icon}]" if content_type == active else icon
        builder.button(text=btn_text, callback_data=safe_callback("filter", content_type.value, query))

    # All buttons in a single compact row
    builder.adjust(8)
    return builder.as_markup()


def build_filter_keyboard_with_label(query: str, active: ContentType = ContentType.ALL) -> InlineKeyboardMarkup:
    """
    Variant with labels next to the icons (two rows: icons + names).
    Used when the screen is wide enough.
    """
    builder = InlineKeyboardBuilder()

    for icon, label, content_type in FILTERS:
        is_active = content_type == active
        btn_text = f"{icon} {label}" if is_active else icon
        callback = f"filter:{content_type.value}:{query}"
        builder.button(text=btn_text, callback_data=callback)

    builder.adjust(4, 4)
    return builder.as_markup()
