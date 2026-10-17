"""
Inline keyboard under search results.

Rows:
  1) content-type filters (8 icons, the active one in brackets); order in category_menu;
  2) sorting: a 📊 sort header label (inactive) + 🎯 relevance / 👀 views / 📅 new
     (exactly ONE active, in brackets);
  3) the 18+ toggle on its OWN full-width row: "🔞 Filter" (off) / "18+ only" (on).
     This way the label fits entirely WITHOUT removing buttons (in a shared row of 5
     the toggle got 1/5 of the width and was truncated);
  4) pagination (◀️ [X/N] ▶️).

The "hot queries" block was REMOVED entirely (privacy): a top-queries list would expose
personal searches while the user base is small. Instead, the results text carries a
link to the bot (formatter.BOT_LINK).
"""

from aiogram.enums import ButtonStyle
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.services.search_engine import ContentType
from bot.utils.helpers import safe_callback

# Sort switch: exactly ONE is active. One emoji per button, exact codepoints:
# 🎯 relevance (U+1F3AF) · 👀 views (U+1F440, two eyes, NOT 👁 U+1F441) ·
# 📅 new (U+1F4C5). The row label is 📊 (U+1F4CA).
_SORTS = [("r", "🎯"), ("v", "👀"), ("n", "📅")]


def build_full_keyboard(
    query: str,
    current_page: int,
    has_more: bool,
    content_type: ContentType,
    sort: str = "r",
    adult_only: bool = False,
) -> InlineKeyboardMarkup:
    """
    Full keyboard under search results (international UI, English).

    sort: "r" relevance (default) / "v" views / "n" new. The active mode is in brackets.
    It is stored in the callback (not in FSM), so every new text query starts with relevance.
    adult_only: 18+ mode toggle state (FSM, kept between queries); only rendered here.
    """
    from bot.keyboards.category_menu import FILTERS

    builder = InlineKeyboardBuilder()
    ct = content_type.value

    # Row 1: filters (8 icons). The active filter is in brackets (independent of sorting).
    for icon, _label, ftype in FILTERS:
        btn_text = f"[{icon}]" if ftype == content_type else icon
        builder.button(
            text=btn_text,
            callback_data=safe_callback("filter", ftype.value, query),
        )

    # Row 2: the "📊 sort" header label (inactive, noop) + the sort switch.
    # (The label is kept on purpose: the toggle width problem is solved by the layout
    # below, WITHOUT removing buttons.)
    builder.button(text="📊 sort", callback_data="noop")
    for code, icon in _SORTS:
        btn_text = f"[{icon}]" if sort == code else icon
        builder.button(text=btn_text, callback_data=safe_callback("sort", code, ct, query))

    # Row 3: the 18+ toggle on its OWN row, alone and full-width, so the label always fits
    # (in a shared row of 5 buttons the toggle got 1/5 of the width and its text was
    # truncated). Off (normal): "🔞 Filter", green; on (18+ only): "18+ only", red.
    # The state is visible by both colour and text.
    # The target mode is NOT encoded in the callback: the handler inverts the current FSM value.
    if adult_only:
        adult_text, adult_style = "18+ only", ButtonStyle.DANGER
    else:
        adult_text, adult_style = "🔞 Filter", ButtonStyle.SUCCESS
    builder.button(
        text=adult_text,
        style=adult_style,
        callback_data=safe_callback("adult", ct, sort, query),
    )

    # Row 4: pagination WITHOUT a page total (the expensive COUNT was removed). Shows the
    # current page number + "◀️"/"▶️". "▶️" is shown while a next page exists (has_more,
    # via fetching N+1) and disappears on the last one. A total "X/Y" is NOT shown (not computed).
    has_prev = current_page > 1
    nav_count = 1
    if has_prev:
        builder.button(text="◀️", callback_data=safe_callback("page", str(current_page - 1), ct, sort, query))
        nav_count += 1
    builder.button(text=f"· {current_page} ·", callback_data="noop")
    if has_more:
        builder.button(text="▶️", callback_data=safe_callback("page", str(current_page + 1), ct, sort, query))
        nav_count += 1

    # Layout: 8 filters | (📊 sort + 3 sorts = 4) | 18+ (1, full width) | navigation.
    # All buttons are kept; the 18+ toggle sits on its OWN row so that the "18+ only" /
    # "🔞 Filter" label fits entirely (solved by layout, not by removing buttons).
    builder.adjust(8, 4, 1, nav_count)
    return builder.as_markup()
