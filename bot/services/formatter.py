"""
Search result formatting for TeleSeek.
A compact, one-line-per-result list: icon, linked title, short description, audience size.

Example output:
    🔍 TeleSeek
    oriental cuisine

    📢 Future Tech │ AI news+...  43k
    📢 Crypto/Bitcoin/News...  39k
    👥 Cooking & Recipes  13k
    📢 Movies│Series  2k

    🔗 link to the bot
"""

import html
import os
import re
from datetime import datetime
from typing import Callable, List, Optional

from bot.database.models import Channel, ChannelType, Media, Post
from bot.i18n import t
from bot.services.search_engine import ContentType, SearchResult

# Link to the bot, shown at the end of every result message. The public username
# comes from BOT_PUBLIC_USERNAME; when it is empty, bot startup fills it in with the
# bot's own username (see set_bot_username / bot.main.on_startup).


def _bot_link(username: str) -> str:
    username = (username or "").strip().lstrip("@")
    return f"🔗 t.me/{username}" if username else ""


BOT_LINK = _bot_link(os.getenv("BOT_PUBLIC_USERNAME", ""))


def set_bot_username(username: str) -> None:
    """Set the bot link from the bot's own username (used when the env var is empty)."""
    global BOT_LINK
    BOT_LINK = _bot_link(username)

# Highlighter type: raw text -> HTML (escaped, with query words wrapped in <b>).
Highlighter = Callable[[Optional[str]], str]


def _build_highlighter(query: str) -> Highlighter:
    """
    Return a function that ESCAPES text (HTML parse_mode) and wraps query words in <b>.
    Light morphology: we match the word STEM (minus up to 2 trailing letters) plus any
    trailing word characters, so inflected forms of a query word are highlighted too
    (e.g. "search" also highlights "searches"/"searching").

    Markup safety: matches are found in the RAW text, and the PIECES (before/match/after)
    are escaped separately, so `<b>` never lands INSIDE an HTML entity (`&amp;` etc.)
    and the markup never breaks.
    """
    stems: List[str] = []
    for w in re.findall(r"\w+", (query or "").lower(), re.UNICODE):
        if len(w) < 3:
            continue
        stem = re.escape(w[: max(3, len(w) - 2)])
        stems.append(stem + r"\w*")
    if not stems:
        return lambda s: html.escape(s or "")

    pat = re.compile("(" + "|".join(stems) + ")", re.IGNORECASE | re.UNICODE)

    def hl(s: Optional[str]) -> str:
        s = s or ""
        out: List[str] = []
        last = 0
        for m in pat.finditer(s):
            out.append(html.escape(s[last:m.start()]))
            out.append("<b>" + html.escape(m.group(0)) + "</b>")
            last = m.end()
        out.append(html.escape(s[last:]))
        return "".join(out)

    return hl


def _plain(s: Optional[str]) -> str:
    """Plain escaping (no highlighting)."""
    return html.escape(s or "")


# ─────────────────────────────────────────────
# Formatting utilities
# ─────────────────────────────────────────────

def format_count(n: int) -> str:
    """
    Abbreviate a subscriber/view count.

    >>> format_count(43000)  → '43k'
    >>> format_count(1200000)  → '1.2M'
    >>> format_count(264)  → '264'
    """
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{v:.1f}M" if v % 1 != 0 else f"{int(v)}M"
    if n >= 1_000:
        v = n / 1_000
        return f"{v:.1f}k" if v % 1 != 0 else f"{int(v)}k"
    return str(n)


def truncate(text: str, max_length: int, suffix: str = "...") -> str:
    """
    Truncate text to max_length characters and append a suffix.

    >>> truncate("A very long channel title", 15)  → 'A very long cha...'
    """
    text = text.replace("\n", " ").strip()
    if len(text) <= max_length:
        return text
    return text[:max_length] + suffix


def format_date(dt: Optional[datetime]) -> str:
    """Format a date: datetime -> '25.03.2026'."""
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y")


# ─────────────────────────────────────────────
# Channel and group formatting
# ─────────────────────────────────────────────

def format_channel_line(channel: Channel, hl: Highlighter = _plain) -> str:
    """
    One result line for a channel/group (HTML, escaped; query words in bold).
    Format: {icon} {linked title} │ {description}...  {subscribers}
    """
    icon = "📢" if channel.type == ChannelType.channel else "👥"

    # Clickable title (title: escaped + highlighted; URL: escaped).
    title_html = hl(channel.title)
    if channel.username:
        url = html.escape(f"https://t.me/{channel.username}")
        name = f'<a href="{url}">{title_html}</a>'
    elif channel.invite_link:
        name = f'<a href="{html.escape(channel.invite_link)}">{title_html}</a>'
    else:
        name = title_html

    desc_part = ""
    if channel.description:
        desc_part = f" │ {hl(truncate(channel.description, 28))}"

    count = format_count(channel.members_count)
    return f"{icon} {name}{desc_part}  {count}"


def format_channels_block(channels: List[Channel], hl: Highlighter = _plain) -> str:
    """Block of lines for a list of channels/groups."""
    return "\n".join(format_channel_line(ch, hl) for ch in channels)


# ─────────────────────────────────────────────
# Post formatting
# ─────────────────────────────────────────────

def format_post_line(post: Post, icon: str = "📝", hl: Highlighter = _plain) -> str:
    """
    One result entry for a post.
    Format:
        {icon} <a href="...">Post text...</a>     <- if the channel has a public username
        {icon} Post text...                        <- no username (no link is built)
           👀 1.2k  🔁 340  📅 25.03.2026

    A post link is built ONLY for channels with a public username:
        https://t.me/{username}/{message_id}

    Channels WITHOUT a public username (username=NULL) are shown without a link to
    the specific post. The https://t.me/c/<internal_id>/<msg_id> format is
    intentionally NOT used: t.me/c/ links only open for channel members, so for a
    regular search-bot user it is a broken link ("private channel / unavailable").
    A broken link is worse than no link.

    Requires post.channel to be loaded (selectinload in queries.py).
    """
    text_preview = truncate(post.text or "No text", 100)
    text_html = hl(text_preview)
    views = format_count(post.views_count)
    forwards = format_count(post.forwards_count)
    date_str = format_date(post.post_date)

    # Post link: public channels only (username present).
    url: Optional[str] = None
    try:
        ch = post.channel
        if ch is not None and ch.username and post.telegram_message_id:
            url = html.escape(f"https://t.me/{ch.username}/{post.telegram_message_id}")
    except Exception:
        # If channel is not loaded, no link is built; just show the text.
        url = None

    if url:
        line1 = f'{icon} <a href="{url}">{text_html}</a>'
    else:
        line1 = f"{icon} {text_html}"

    line2 = f"   👀 {views}  🔁 {forwards}  📅 {date_str}"
    return f"{line1}\n{line2}"


def format_posts_block(posts: List[Post], icon: str = "📝", hl: Highlighter = _plain) -> str:
    """Block of entries for a list of posts."""
    return "\n\n".join(format_post_line(p, icon=icon, hl=hl) for p in posts)


# ─────────────────────────────────────────────
# Media formatting
# ─────────────────────────────────────────────

_MEDIA_ICONS = {
    # MediaType values (for format_media_line)
    "photo": "🖼",
    "video": "🎬",
    "audio": "🎧",
    "document": "📁",
    # ContentType values (post icon in media filters)
    "photos": "🖼",
    "videos": "🎬",
    "documents": "📁",
    # "audio" is already covered above (ContentType.AUDIO.value == "audio")
}


def format_media_line(media: Media) -> str:
    """One result line for a media file."""
    icon = _MEDIA_ICONS.get(media.type.value, "📎")
    caption = truncate(media.caption or media.file_name or "No caption", 50)
    return f"{icon} {_plain(caption)}"


# ─────────────────────────────────────────────
# Hot queries
# ─────────────────────────────────────────────

# Hot queries (format_hot_*) were REMOVED for privacy reasons. BOT_LINK is shown
# at the end of the results instead.


# ─────────────────────────────────────────────
# Main search result formatter
# ─────────────────────────────────────────────

def format_search_results(
    result: SearchResult,
    content_type: ContentType = ContentType.ALL,
    lang: str = "en",
) -> str:
    """
    Full formatting of search results (international UI).

    Translatable strings (Nothing found / cap hint) go through t(key, lang). Technical
    elements do NOT depend on the language: "Page X", the bot link, the "TeleSeek" brand,
    type/sort/18+ icons. The filter label (📢 Channels ...) is universal, like menu icons.

    Message structure:
        🔍 TeleSeek

        {user query} · {filter type, if not ALL}

        {list of channels/groups/posts/media}

        Page X (if applicable)
        🔗 link to the bot

    Args:
        result: SearchResult object from SearchEngine.
        content_type: Active filter (to display the type).
        lang: UI language (for translatable result strings).

    Returns:
        Ready-to-send HTML string for Telegram.
    """
    hl = _build_highlighter(result.query)
    lines: List[str] = []

    # Header (concise, English). The query is escaped; the filter type is in English.
    lines.append("🔍 <b>TeleSeek</b>")
    head = f"<i>{_plain(result.query)}</i>"
    if content_type != ContentType.ALL:
        head += f" · {_content_type_label(content_type)}"
    lines.append(head)
    lines.append("")

    # No results
    if not result.has_results:
        lines.append(t("nothing_found", lang))
        lines.append("")
        lines.append(BOT_LINK)
        return "\n".join(lines)

    # Channels and groups (query words in bold)
    if result.channels:
        lines.append(format_channels_block(result.channels, hl))

    # Posts (including posts from media filters)
    if result.posts:
        if result.channels:
            lines.append("")
        post_icon = _MEDIA_ICONS.get(content_type.value, "📝")
        lines.append(format_posts_block(result.posts, icon=post_icon, hl=hl))

    # Media (legacy path, kept in case of direct use)
    if result.media:
        if result.channels or result.posts:
            lines.append("")
        for m in result.media:
            lines.append(format_media_line(m))

    # Pagination WITHOUT a total (no COUNT): only the page number.
    # "X / Y" (total) is not shown; "next" is a keyboard button (has_more).
    if result.page > 1 or result.has_more:
        lines.append(f"\n<i>Page {result.page}</i>")

    # MAX_RESULTS limit reached: briefly suggest refining the query.
    if result.capped:
        lines.append(f"<i>{t('capped_hint', lang)}</i>")

    # Instead of "hot queries" (hidden), a short link to the bot.
    lines.append("")
    lines.append(BOT_LINK)

    return "\n".join(lines)


def format_no_results(query: str, lang: str = "en") -> str:
    """Standalone "no results" message (in the user's language)."""
    return "\n".join([
        "🔍 <b>TeleSeek</b>",
        f"<i>{_plain(query)}</i>",
        "",
        t("nothing_found", lang),
        "",
        BOT_LINK,
    ])


# ─────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────

_CONTENT_TYPE_LABELS = {
    ContentType.CHANNELS:  "📢 Channels",
    ContentType.GROUPS:    "👥 Groups",
    ContentType.POSTS:     "📂 Posts",
    ContentType.PHOTOS:    "🖼 Photos",
    ContentType.VIDEOS:    "🎬 Videos",
    ContentType.AUDIO:     "🎧 Audio",
    ContentType.DOCUMENTS: "📁 Files",
    ContentType.ALL:       "",
}


def _content_type_label(content_type: ContentType) -> str:
    """Human-readable content type name shown in the results."""
    return _CONTENT_TYPE_LABELS.get(content_type, "")
