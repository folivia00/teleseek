"""
TeleSeek search query handlers.
Accepts text messages, runs the search and renders results with keyboards.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import i18n
from bot.database import queries
from bot.database.connection import AsyncSessionFactory
from bot.i18n import t
from bot.limits import write_interaction
from bot.safety import BLOCKED_LOG_MARKER, BlockedQueryError, is_blocked_query
from bot.keyboards.main_menu import build_main_reply_keyboard
from bot.keyboards.pagination import build_full_keyboard
from bot.services.formatter import format_search_results
from bot.services.search_engine import ContentType, SearchEngine, SearchResult
from bot.utils.helpers import is_valid_query, sanitize_query

logger = logging.getLogger(__name__)

router = Router(name="search")

# Menu reply buttons are NOT searched as a query: their text triggers an action instead
# of going to search. The label is recognised in ANY language (i18n.all_texts across
# all SUPPORTED_LANGS) + legacy Russian labels, in case of a stale reply keyboard.
_MENU_SEARCH = i18n.all_texts("btn_search") | {"🔍 Поиск"}
_MENU_HELP = i18n.all_texts("btn_help") | {"📖 Помощь"}
_MENU_LANGUAGE = i18n.all_texts("btn_language")


# ─────────────────────────────────────────────
# Response-building helper
# ─────────────────────────────────────────────

async def _build_response(
    session: AsyncSession,
    query: str,
    content_type: ContentType,
    page: int,
    sort: str = "r",
    adult_only: bool = False,
    lang: str = "en",
) -> tuple[str, object, SearchResult]:
    """
    Run the search and build the reply text + keyboard.

    sort: "r" relevance (DEFAULT) / "n" new (date) / "v" views.
    It is stored in the callback (not in FSM), so every NEW query starts with relevance.
    adult_only: 18+ mode toggle (False = default normal: everything except 18+;
    True = 18+ only).
    lang: UI language for translatable result strings; icons/"Page X" are universal
    and do NOT depend on the language.
    Returns:
        Tuple (text, keyboard, result); result is needed for the query log (analytics).

    Raises:
        BlockedQueryError: the query is blocklisted; NOT A SINGLE DB call is made.
        The check HERE covers ALL search paths at once: new query, pagination,
        filter/sort change, 18+ toggle (every handler builds its reply only through
        this function), including old keyboards issued before the blocklist existed
        and forged callback_data.
    """
    if is_blocked_query(query):
        raise BlockedQueryError
    engine = SearchEngine(session)

    if content_type == ContentType.ALL:
        result = await engine.search_all(query, page=page, adult_only=adult_only, sort=sort)
    else:
        result = await engine.search_by_type(
            query, content_type, page=page, sort=sort, adult_only=adult_only
        )

    text = format_search_results(result, content_type, lang=lang)
    keyboard = build_full_keyboard(
        query=query,
        current_page=result.page,
        has_more=result.has_more,   # no total; "next" shown only if more exist (N+1)
        content_type=content_type,
        sort=sort,
        adult_only=adult_only,
    )
    return text, keyboard, result


# ─── The content filter (18+ mode) lives in the USER's FSM state ──────────────
# The mode used to be baked into the callback of each result message (an `m` flag),
# so a NEW search query reset the filter to normal (a UX bug). Now the mode is a
# single per-user state (aiogram FSM, MemoryStorage): it persists between queries
# within a session but resets to normal on bot restart (a safe default).
# FSM is the single source of truth for the mode; callbacks no longer carry a mode flag.
_MODE_KEY = "adult_only"


async def _get_mode(state: FSMContext) -> bool:
    """The user's current 18+ mode (defaults to False = normal if unset)."""
    data = await state.get_data()
    return bool(data.get(_MODE_KEY, False))


async def _set_mode(state: FSMContext, adult_only: bool) -> None:
    """Save the user's 18+ mode (kept until changed or the bot restarts)."""
    await state.update_data(**{_MODE_KEY: adult_only})


# ─────────────────────────────────────────────
# Text search
# ─────────────────────────────────────────────
# The PUBLIC top-queries list (/hot, "Hot", hot_searches increment) was REMOVED (privacy:
# other people's searches are never revealed to users). INTERNAL demand analytics remain:
# queries are written to search_log and are visible ONLY to admins in the internal admin
# panel ("demand" section, i.e. gaps in the index). Users see no public top list.

@router.message(F.text & ~F.text.startswith("/"))
async def handle_search_query(
    message: Message, session: AsyncSession, state: FSMContext, lang: str = "en"
) -> None:
    """
    Main search handler.
    Any text message (not a command) -> search query.
    The 18+ mode comes from the user's saved state (kept between queries).
    Texts are in the user's language (lang from middleware).
    """
    raw = (message.text or "").strip()

    # Menu reply-button text is NOT searched; the button's action is performed instead.
    # The label is recognised in any language (i18n.all_texts) + legacy Russian (see _MENU_*).
    if raw in _MENU_SEARCH:
        await message.answer(t("search_prompt", lang))
        return
    if raw in _MENU_HELP:
        await message.answer(t("help", lang))
        return
    # "🌐 Language" button: cycle through SUPPORTED_LANGS, SAVE it per user, and
    # immediately re-render the welcome text + reply keyboard in the new language.
    if raw in _MENU_LANGUAGE:
        new_lang = i18n.next_lang(lang)
        if message.from_user is not None:
            await queries.set_user_language(session, message.from_user.id, new_lang)
        await message.answer(
            t("lang_switched", new_lang),
            reply_markup=build_main_reply_keyboard(new_lang),
        )
        await message.answer(t("welcome", new_lang))
        return

    query = sanitize_query(message.text or "")

    if not is_valid_query(query):
        await message.answer(t("invalid_query", lang))
        return

    if len(query) > 100:
        await message.answer(t("query_too_long", lang))
        return

    # Blocklist: an early guard, BEFORE "Searching..." and before any search query to
    # the DB. Only the fact (a marker) is written to search_log, NOT the query text;
    # has_results=True on purpose (False would surface it in the admin panel's "index
    # gaps" as demand; see bot/safety.py).
    if is_blocked_query(query):
        await queries.record_search_log(
            session,
            telegram_user_id=message.from_user.id if message.from_user else None,
            query=BLOCKED_LOG_MARKER,
            results_count=0,
            has_results=True,
        )
        await message.answer(t("query_blocked", lang))
        return

    searching_msg = await message.answer(t("searching", lang))

    try:
        adult_only = await _get_mode(state)
        text, keyboard, result = await _build_response(
            session, query, ContentType.ALL, page=1, adult_only=adult_only, lang=lang
        )

        # INTERNAL demand analytics: the query is written to search_log, visible ONLY in
        # the internal admin panel ("demand" section) and never shown to users.
        # has_results=False = a gap in the index (content to collect). The exact total
        # is NOT computed (no COUNT); results_count = results on the page (a lower bound,
        # as a presence signal). The public top list stays removed.
        results_count = len(result.channels) + len(result.posts)
        await queries.record_search_log(
            session,
            telegram_user_id=message.from_user.id if message.from_user else None,
            query=query,
            results_count=results_count,
            has_results=result.has_results,
            adult_only=adult_only,   # 18+ breakdown in the admin panel "Demand" view
        )

        await searching_msg.delete()
        sent = await message.answer(
            text, reply_markup=keyboard, disable_web_page_preview=True
        )

        # The new result message becomes the user's ONLY active search session.
        # Order matters: first disable the keyboard of the PREVIOUS result (while it is
        # still registered as active), then register the new one.
        if message.from_user is not None:
            await _retire_previous_session(message, session)
            await queries.start_search_session(
                session,
                telegram_user_id=message.from_user.id,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
            )

    except BlockedQueryError:
        # Safety net (the main guard is above, before "Searching..."): neutral reply,
        # the query text is NOT logged.
        await searching_msg.edit_text(t("query_blocked", lang))
    except Exception as e:
        logger.error("Search failed for query %r: %s", query, e)
        await searching_msg.edit_text(t("search_error", lang))


# ─────────────────────────────────────────────
# Callback handlers
# ─────────────────────────────────────────────

async def _retire_previous_session(message: Message, session: AsyncSession) -> None:
    """Remove the keyboard from the user's PREVIOUS result message (cosmetic).

    Why: the on-press guard (``_guard_active_session``) is the mandatory part, but
    without this the old message's buttons would still LOOK usable. They are removed
    right away on a new query so the UI state matches the real one.

    Strictly best-effort: Telegram does not allow editing messages older than 48 hours,
    deleted ones, or ones already without a keyboard. No error here may interfere with
    delivering the new result, so errors are swallowed; the on-press guard remains the
    authoritative check.
    """
    if message.from_user is None or message.bot is None:
        return
    try:
        prev = await queries.get_active_session_message(session, message.from_user.id)
        if prev is None:
            return
        chat_id, message_id = prev
        await message.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except Exception:
        pass


async def _guard_active_session(
    callback: CallbackQuery, session: AsyncSession, lang: str
) -> bool:
    """True -> the message is ACTIVE, continue processing. False -> the session has ended.

    Only the user's LATEST result message is active. The session identifier is the
    (chat_id, message_id) pair taken from the Telegram callback update itself, so a
    stale or forged ``callback_data`` cannot bypass the restriction: it does not
    contain that pair.

    On rejection: no search is performed (not a single content query to the DB), the
    user gets a friendly popup explanation, and the old message's keyboard is removed
    so the buttons stop looking usable. Removal is best-effort: Telegram does not allow
    editing messages older than 48 hours or deleted ones, and that must NOT break the reply.
    """
    msg = callback.message
    user = callback.from_user
    if msg is None or user is None:            # nothing to check without a message
        return True

    if await queries.is_active_session(session, user.id, msg.chat.id, msg.message_id):
        return True

    await callback.answer(t("session_expired", lang), show_alert=True)
    try:
        await msg.edit_reply_markup(reply_markup=None)
    except Exception:
        pass                                   # old/deleted message: not critical
    # UX signal: how often people run into an ended session.
    await write_interaction(AsyncSessionFactory, user.id, "expired")
    return False


async def _answer_blocked_callback(
    callback: CallbackQuery, session: AsyncSession, lang: str
) -> None:
    """Single reply to a blocked query coming through a callback path.

    Possible with old keyboards (issued before the blocklist existed) and with forged
    callback_data. The results are replaced with a neutral text WITHOUT a keyboard
    (buttons carrying the old query disappear); search_log gets only the fact (a
    marker), NOT the text."""
    await queries.record_search_log(
        session,
        telegram_user_id=callback.from_user.id if callback.from_user else None,
        query=BLOCKED_LOG_MARKER,
        results_count=0,
        has_results=True,
    )
    if callback.message is not None:
        await callback.message.edit_text(t("query_blocked", lang))
    await callback.answer()


@router.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery) -> None:
    """Informational buttons with no action (e.g. the page counter)."""
    await callback.answer()


@router.callback_query(F.data.startswith("filter:"))
async def handle_filter_callback(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, lang: str = "en"
) -> None:
    """
    Content-type filter button press.
    Format: filter:<content_type>:<query>. The 18+ mode comes from user state (not the callback).
    """
    parts = callback.data.removeprefix("filter:").split(":", 1)
    if len(parts) != 2:
        await callback.answer()
        return

    content_type_str, query = parts[0], parts[1]

    try:
        content_type = ContentType(content_type_str)
    except ValueError:
        await callback.answer("Unknown filter type")
        return

    # Guard order: the blocklist FIRST (a cheap in-memory check that lets no forbidden
    # query through by any path), then session activity.
    if is_blocked_query(query):
        await _answer_blocked_callback(callback, session, lang)
        return
    if not await _guard_active_session(callback, session, lang):
        return

    adult_only = await _get_mode(state)
    try:
        text, keyboard, result = await _build_response(
            session, query, content_type, page=1, adult_only=adult_only, lang=lang
        )
    except BlockedQueryError:
        await _answer_blocked_callback(callback, session, lang)
        return
    await callback.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    await callback.answer()
    # Telemetry: AFTER the reply, in its own short transaction (no added latency).
    await write_interaction(
        AsyncSessionFactory, callback.from_user.id if callback.from_user else None, "filter",
        content_type=content_type.value, page=1, adult_only=adult_only,
        is_last_page=not result.has_more,
    )


@router.callback_query(F.data.startswith("page:"))
async def handle_page_callback(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, lang: str = "en"
) -> None:
    """
    Result pagination.
    Format: page:<page_num>:<content_type>:<sort>:<query>
    sort: "r" relevance / "n" new / "v" views. The 18+ mode comes from user state.
    """
    parts = callback.data.removeprefix("page:").split(":", 3)
    if len(parts) != 4:
        await callback.answer()
        return

    try:
        page = int(parts[0])
        content_type = ContentType(parts[1])
        sort = parts[2] if parts[2] in ("r", "n", "v") else "r"
        query = parts[3]
    except (ValueError, KeyError):
        await callback.answer()
        return

    # Guards: blocklist -> active session.
    if is_blocked_query(query):
        await _answer_blocked_callback(callback, session, lang)
        return
    if not await _guard_active_session(callback, session, lang):
        return

    adult_only = await _get_mode(state)
    try:
        text, keyboard, result = await _build_response(
            session, query, content_type, page=page, sort=sort, adult_only=adult_only, lang=lang
        )
    except BlockedQueryError:
        await _answer_blocked_callback(callback, session, lang)
        return
    await callback.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    await callback.answer()
    # Telemetry: the KEY signal is paging depth (page) and reaching the end (is_last_page).
    await write_interaction(
        AsyncSessionFactory, callback.from_user.id if callback.from_user else None, "page",
        content_type=content_type.value, sort=sort, page=page, adult_only=adult_only,
        is_last_page=not result.has_more,
    )


@router.callback_query(F.data.startswith("sort:"))
async def handle_sort_callback(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, lang: str = "en"
) -> None:
    """
    Result sort switch: relevance / new / views, exactly ONE active.
    Format: sort:<code>:<content_type>:<query>, code ∈ {r,n,v}. Resets to page 1.
    The 18+ mode comes from FSM (not reset). Sorting lives in the callback (not FSM),
    so every new text query starts with relevance.
    """
    parts = callback.data.removeprefix("sort:").split(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return
    code = parts[0] if parts[0] in ("r", "n", "v") else "r"
    try:
        content_type = ContentType(parts[1])
    except ValueError:
        await callback.answer()
        return
    query = parts[2]

    # Guards: blocklist -> active session.
    if is_blocked_query(query):
        await _answer_blocked_callback(callback, session, lang)
        return
    if not await _guard_active_session(callback, session, lang):
        return

    adult_only = await _get_mode(state)
    try:
        text, keyboard, result = await _build_response(
            session, query, content_type, page=1, sort=code, adult_only=adult_only, lang=lang
        )
    except BlockedQueryError:
        await _answer_blocked_callback(callback, session, lang)
        return
    await callback.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    # Sort toast: the icon is universal, the word is in the user's language.
    await callback.answer(f"{t('sort_label', lang)}: {t('sort_' + code, lang)}")
    await write_interaction(
        AsyncSessionFactory, callback.from_user.id if callback.from_user else None, "sort",
        content_type=content_type.value, sort=code, page=1, adult_only=adult_only,
        is_last_page=not result.has_more,
    )


@router.callback_query(F.data.startswith("adult:"))
async def handle_adult_toggle_callback(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, lang: str = "en"
) -> None:
    """
    18+ mode toggle (normal <-> adult_only). FLIPS the user's saved mode (kept for
    subsequent queries) and rebuilds the current results (page 1).
    Format: adult:<content_type>:<sort>:<query> (the target mode is NOT in the callback:
    the current one is read from FSM and inverted; FSM is the single source of truth).
    """
    parts = callback.data.removeprefix("adult:").split(":", 2)
    if len(parts) != 3:
        await callback.answer()
        return

    try:
        content_type = ContentType(parts[0])
        sort = parts[1] if parts[1] in ("r", "n", "v") else "r"
        query = parts[2]
    except (ValueError, KeyError):
        await callback.answer()
        return

    # Guards: blocklist -> active session. IMPORTANT: they run before the 18+ mode
    # changes, so a press on an old result message never alters the user's saved mode.
    if is_blocked_query(query):
        await _answer_blocked_callback(callback, session, lang)
        return
    if not await _guard_active_session(callback, session, lang):
        return

    adult_only = not await _get_mode(state)   # invert the current mode
    await _set_mode(state, adult_only)         # save it; kept for subsequent queries
    try:
        text, keyboard, result = await _build_response(
            session, query, content_type, page=1, sort=sort, adult_only=adult_only, lang=lang
        )
    except BlockedQueryError:
        await _answer_blocked_callback(callback, session, lang)
        return
    await callback.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    await callback.answer(t("adult_on", lang) if adult_only else t("adult_off", lang))
    # Toggle event (search_log only shows the STATE at search time, not the switch itself).
    await write_interaction(
        AsyncSessionFactory, callback.from_user.id if callback.from_user else None, "adult",
        content_type=content_type.value, sort=sort, page=1, adult_only=adult_only,
        is_last_page=not result.has_more,
    )
