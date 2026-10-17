"""
Internationalisation (i18n) of the TeleSeek bot UI.

All TRANSLATABLE UI texts of the bot (welcome, help, reply buttons, toasts/errors)
live here in a single extensible dictionary and are fetched via ``t(key, lang)``.
Search logic and result data do NOT depend on the language.

Extensibility: adding a language = adding its code to ``SUPPORTED_LANGS`` and the
values to ``TRANSLATIONS`` (keys stay the same). Handlers do NOT need rewriting, since
they only go through ``t()``. Missing translation -> fallback to ``DEFAULT_LANG`` (en),
then to the key itself (so a missing key never crashes the bot).

IMPORTANT: what is NEVER translated (identical in every language, hence NOT here):
  * sort menu icons (📊 🎯 👀 📅) and media filter icons (🌐 📢 👥 💬 🎬 🖼 🎧 📁);
  * the 18+ mark (🔞);
  * technical result strings ("Page X"), the bot link.
Menu emoji appear INSIDE translatable texts (welcome/help) only to illustrate the
features; only the wording next to them is translated, not the icons themselves.
"""

from typing import Optional

# Default (fallback) language and the list of supported ones. To extend, add the code
# here and the values in TRANSLATIONS. The order defines the "🌐 Language" button cycle.
DEFAULT_LANG: str = "en"
SUPPORTED_LANGS: tuple[str, ...] = ("en", "ru")


# ─────────────────────────────────────────────────────────────────────────────
# Translation dictionary: key -> {lang: text}
# ─────────────────────────────────────────────────────────────────────────────
# New language: add a value to each key (or at least where needed; anything missing
# falls back to en). HTML markup (<b>/<i>) is allowed since parse_mode=HTML.
TRANSLATIONS: dict[str, dict[str, str]] = {
    # ── Welcome (/start). Menu lines are separated by blank lines; one line describes
    #    the 18+ toggle. The button name matches the real bot button "🔞 Filter"
    #    (pagination.py). EN is a translation of the same text.
    "welcome": {
        "en": (
            "🔍 <b>TeleSeek</b> — Telegram search\n"
            "\n"
            "Just type a word — I'll find the best matches across channels, "
            "groups and posts.\n"
            "\n"
            "After searching, refine via the menu:\n"
            "\n"
            "🌐📢👥💬🎬🖼🎧📁 — filter by type\n"
            "\n"
            "📊 🎯 👀 📅 — sorting (relevance / views / date)\n"
            "\n"
            "🟢\"🔞 Filter\" - 18+ content filtering (ON/OFF)🟢/🔴\n"
            "\n"
            "👇 Type your query to start."
        ),
        "ru": (
            "🔍 <b>TeleSeek</b> — поиск по Telegram\n"
            "\n"
            "Просто напиши слово — найду лучшие совпадения в каналах, "
            "группах и постах.\n"
            "\n"
            "После поиска уточняй через меню:\n"
            "\n"
            "🌐📢👥💬🎬🖼🎧📁 — фильтр по типу\n"
            "\n"
            "📊 🎯 👀 📅 — сортировка (релевантность / просмотры / дата)\n"
            "\n"
            "🟢\"🔞 Filter\" - фильтрация 18+ контента (ON/OFF)🟢/🔴\n"
            "\n"
            "👇 Напиши запрос, чтобы начать."
        ),
    },
    # ── Help (/help). Menu icons are universal; only the description is translated.
    "help": {
        "en": (
            "📖 <b>TeleSeek — Help</b>\n"
            "\n"
            "<b>Search:</b> just type a query (any language).\n"
            "\n"
            "<b>Filters (under results):</b>\n"
            "🌐 all · 📢 channels · 👥 groups · 💬 posts\n"
            "🎬 videos · 🖼 photos · 🎧 audio · 📁 files\n"
            "\n"
            "<b>Sort:</b> 🎯 relevance · 👀 views · 📅 new\n"
            "<b>18+:</b> 🔞 toggle (kept on/off between searches)\n"
            "<b>Pages:</b> ◀️ / ▶️\n"
            "<b>Language:</b> 🌐 switch interface language\n"
            "<b>Feedback:</b> 📝 share your thoughts — we read everything\n"
            "\n"
            "<b>Commands:</b>\n"
            "/start — home\n"
            "/help — this help"
        ),
        "ru": (
            "📖 <b>TeleSeek — Справка</b>\n"
            "\n"
            "<b>Поиск:</b> просто напиши запрос (на любом языке).\n"
            "\n"
            "<b>Фильтры (под результатами):</b>\n"
            "🌐 всё · 📢 каналы · 👥 группы · 💬 посты\n"
            "🎬 видео · 🖼 фото · 🎧 аудио · 📁 файлы\n"
            "\n"
            "<b>Сортировка:</b> 🎯 релевантность · 👀 просмотры · 📅 новые\n"
            "<b>18+:</b> 🔞 переключатель (сохраняется между поисками)\n"
            "<b>Страницы:</b> ◀️ / ▶️\n"
            "<b>Язык:</b> 🌐 сменить язык интерфейса\n"
            "<b>Обратная связь:</b> 📝 поделитесь мнением — мы читаем всё\n"
            "\n"
            "<b>Команды:</b>\n"
            "/start — на главную\n"
            "/help — эта справка"
        ),
    },
    # ── Bottom-menu reply buttons. Emoji are universal; the label is translated.
    "btn_search": {"en": "🔍 Search", "ru": "🔍 Поиск"},
    "btn_help": {"en": "📖 Help", "ru": "📖 Помощь"},
    "btn_language": {"en": "🌐 Language", "ru": "🌐 Язык"},
    "btn_feedback": {"en": "📝 Feedback", "ru": "📝 Обратная связь"},
    # Input field placeholder under the reply keyboard.
    "input_placeholder": {"en": "Type to search...", "ru": "Введите запрос..."},
    # ── Prompt shown on the "🔍 Search" button (waiting for a query).
    "search_prompt": {
        "en": "🔍 Type your query:",
        "ru": "🔍 Введите запрос:",
    },
    # ── Bot toasts/errors.
    "invalid_query": {
        "en": "⚠️ Type a search query.",
        "ru": "⚠️ Введите поисковый запрос.",
    },
    "query_too_long": {
        "en": "⚠️ Query too long. Max 100 characters.",
        "ru": "⚠️ Запрос слишком длинный. Максимум 100 символов.",
    },
    "searching": {"en": "🔍 Searching...", "ru": "🔍 Ищу..."},
    # ── Neutral blocklist reply. INTENTIONALLY gives no reason, no hint on how to
    #    rephrase and no details of the rules.
    "query_blocked": {
        "en": "This query cannot be processed.",
        "ru": "Этот запрос не может быть обработан.",
    },
    # ── Interaction with an OUTDATED result message. Neutral, friendly tone: this is
    #    not a user error but the normal end of a previous session. Shown as a popup
    #    (show_alert) when buttons under a stale message are pressed.
    "session_expired": {
        "en": "This search session has ended. Send a new query to search again.",
        "ru": "Эта поисковая сессия завершена. Отправьте новый запрос, чтобы искать снова.",
    },
    "search_error": {
        "en": "❌ Search error. Please try again later.",
        "ru": "❌ Ошибка поиска. Попробуйте позже.",
    },
    # ── Result texts (result format). "Page X" is NOT here: it is technical.
    "nothing_found": {
        "en": "😕 Nothing found. Try another query.",
        "ru": "😕 Ничего не найдено. Попробуйте другой запрос.",
    },
    "capped_hint": {
        "en": "💡 Showing top results — refine your query for more precise matches.",
        "ru": "💡 Показаны лучшие результаты — уточните запрос для более точного совпадения.",
    },
    # ── Sort switch toasts (the icon is universal, the word is translated).
    "sort_label": {"en": "Sort", "ru": "Сортировка"},
    "sort_r": {"en": "🎯 Relevance", "ru": "🎯 Релевантность"},
    "sort_v": {"en": "👀 Views", "ru": "👀 Просмотры"},
    "sort_n": {"en": "📅 New", "ru": "📅 Новые"},
    # ── 18+ mode toggle toasts (the 🔞 icon is universal).
    "adult_on": {"en": "🔞 Mode: 18+ only", "ru": "🔞 Режим: только 18+"},
    "adult_off": {"en": "🔞 Mode: safe (no 18+)", "ru": "🔞 Режим: безопасный (без 18+)"},
    # ── Language switch confirmation (toast + message).
    "lang_switched": {
        "en": "🌐 Language: English",
        "ru": "🌐 Язык: Русский",
    },
    # ── Feedback. The anonymity promise is honest: the user id is NEVER shown anywhere
    #    in the bot; it is stored internally only for moderation (internal admin panel).
    "feedback_prompt": {
        "en": (
            "📝 <b>We'd love to hear from you!</b>\n"
            "\n"
            "Write your feedback or request in one message — every opinion "
            "matters to us 🙏\n"
            "\n"
            "All messages are anonymous: if you'd like a reply, leave your "
            "contact info in the message."
        ),
        "ru": (
            "📝 <b>Нам важно ваше мнение!</b>\n"
            "\n"
            "Напишите отзыв или обращение одним сообщением — мы читаем каждое 🙏\n"
            "\n"
            "Все обращения анонимны: если нужен ответ — оставьте контакты "
            "в сообщении."
        ),
    },
    "feedback_thanks": {
        "en": "💚 Thank you, we've received your message 🙏 It helps us make the search better!",
        "ru": "💚 Спасибо, мы получили ваше сообщение 🙏 Оно поможет сделать поиск лучше!",
    },
    "feedback_rate_limited": {
        "en": "🕐 You can send feedback once per hour. Please try again a bit later 🙏",
        "ru": "🕐 Отправлять обращения можно раз в час. Пожалуйста, попробуйте чуть позже 🙏",
    },
    "feedback_too_long": {
        "en": "⚠️ Message too long. Max 2000 characters — please shorten it a bit.",
        "ru": "⚠️ Сообщение слишком длинное. Максимум 2000 символов — сократите, пожалуйста.",
    },
    "feedback_empty": {
        "en": "⚠️ Please send your feedback as a text message.",
        "ru": "⚠️ Пожалуйста, отправьте обращение текстовым сообщением.",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────

def normalize_lang(lang: Optional[str]) -> str:
    """Normalise a language to a supported code (otherwise DEFAULT_LANG)."""
    if lang and lang in SUPPORTED_LANGS:
        return lang
    return DEFAULT_LANG


def detect_lang(language_code: Optional[str]) -> str:
    """
    Detect the UI language from Telegram ``language_code`` (AUTOMATIC, no prompt).

    Rule: a code starting with ``ru`` -> ru; everything else -> en (default). A new
    user gets a UI right away without a selection screen. To support new languages,
    add branches here and codes to SUPPORTED_LANGS.
    """
    code = (language_code or "").lower()
    if code.startswith("ru"):
        return "ru"
    return DEFAULT_LANG


def t(key: str, lang: Optional[str] = None) -> str:
    """
    Text for a key in language ``lang``.

    Fallback chain: requested language -> DEFAULT_LANG (en) -> the key itself (guards
    against a missing key: the bot does not crash and the gap is visible).
    """
    entry = TRANSLATIONS.get(key)
    if entry is None:
        return key
    lang = normalize_lang(lang)
    if lang in entry:
        return entry[lang]
    if DEFAULT_LANG in entry:
        return entry[DEFAULT_LANG]
    # Last resort: any available value, or the key itself.
    return next(iter(entry.values()), key)


def all_texts(key: str) -> set[str]:
    """
    All text variants of a key across all languages (used to recognise a pressed
    reply button regardless of the user's current language; see search.py).
    """
    return set(TRANSLATIONS.get(key, {}).values())


def next_lang(lang: Optional[str]) -> str:
    """
    Next language in the SUPPORTED_LANGS cycle (for the "🌐 Language" button).

    With two languages this is a simple en<->ru toggle. With more languages it
    becomes a round-robin; a selection-list screen is left for the future.
    """
    cur = normalize_lang(lang)
    idx = SUPPORTED_LANGS.index(cur)
    return SUPPORTED_LANGS[(idx + 1) % len(SUPPORTED_LANGS)]
