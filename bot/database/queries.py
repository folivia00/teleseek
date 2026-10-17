"""
Search queries against the TeleSeek database.
Full-text search via PostgreSQL tsvector/tsquery with an ILIKE fallback.
"""

import logging
import os
from typing import List, Optional

from sqlalchemy import String, Text, and_, case, exists, func, or_, select, text, true
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)

# Result cap for posts/media: dedup and sorting run over the top-N by the active sort
# order rather than the whole match set (removes the load on popular words). Configured
# via the MAX_RESULTS env var. Nobody pages that far — at the cap the bot suggests
# refining the query. The candidate-selection cost is DIRECTLY proportional to this cap
# (top-N is collected by the active sort BEFORE dedup); a few hundred results = dozens
# of pages of 10, well beyond what a real user pages through, so a lower value in the
# bot environment is a reasonable trade-off.
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "500"))

# ─── ILIKE fallback over POSTS: hard time budget ──────────────────────────────
# Problem: the fallback kicks in when tsvector finds NOTHING (a noticeable share of
# queries) and does a seq-scan over millions of posts — tens of seconds per query.
# It also held the transaction open, so a user_stats row lock could hang for minutes
# and cascade into other requests.
#
# Solution: do NOT drop the fallback entirely (it occasionally helps where tsvector
# cannot: substrings inside words/compounds, hashtags, scripts without a stemmer), but
# give it a HARD BUDGET: `SET LOCAL statement_timeout` inside a SAVEPOINT. A cheap
# fallback (dense match — the plan over the sort index stops early) finishes and is
# useful; an expensive one (rare substring → full scan) is cut off by the timeout and
# honestly returns "nothing found". The worst case is bounded by the budget, not minutes.
#
# The SAVEPOINT is mandatory: a statement cancelled by timeout "poisons" the
# transaction — rolling back to the savepoint restores it and the bot replies normally.
# Full kill switch — `POST_ILIKE_FALLBACK=0` (rollback without a redeploy).
POST_ILIKE_FALLBACK = os.getenv("POST_ILIKE_FALLBACK", "1").strip().lower() not in (
    "0", "false", "no", "off", ""
)
POST_FALLBACK_TIMEOUT_MS = int(os.getenv("POST_FALLBACK_TIMEOUT_MS", "2000"))
# A separate (smaller) candidate cap for the fallback: scan cost ∝ cap, and the
# fallback is a last-resort path that does not need deep pagination.
POST_FALLBACK_MAX_RESULTS = int(os.getenv("POST_FALLBACK_MAX_RESULTS", "200"))

from bot.database.models import (
    Channel,
    ChannelType,
    HotSearch,
    Media,
    MediaType,
    Post,
    SearchLog,
    UserStats,
)


def _safe_ts_query(query: str):
    """plainto_tsquery guarded against an empty string via coalesce."""
    return func.plainto_tsquery("russian", func.coalesce(query, ""))


# ─── Multilingual prefix fallback ─────────────────────────────────────────────
# Several agglutinative languages (e.g. Kazakh, Uzbek) have NO stemmer in Postgres, so
# word forms are not collapsed (a base form does not find its plural). Their suffixes
# are strictly at the END of a word, so a prefix query (lexeme:*) works as
# pseudo-stemming (in testing it matched all word forms where the exact query matched
# half). Used ONLY AS A FALLBACK: the exact tsquery returned "few" (≤ threshold) →
# repeat the same path with :*. Russian/English are unaffected — they have plenty of
# exact results, so the fallback does not trigger; noise from short stems is cut off
# by a minimum lexeme length ≥5 (PREFIX_MIN_TOKEN_LEN — in the REGEX below, {5,}).
#
# "Few" = PREFIX_FALLBACK_THRESHOLD (≤ one result page!): the fallback triggers only
# when exact results do not fill even a single page. The decision is DETERMINISTIC
# across pagination: the exact total is known precisely when the exact path returned
# FEWER than limit (then total = offset + len(rows)); if the page is full, there are
# certainly > threshold exact hits → exact mode on every page.
# Prefix set ⊇ exact set (lexeme:* also matches the lexeme itself) → replacing the
# results never loses exact hits.
PREFIX_FALLBACK_THRESHOLD = int(os.getenv("PREFIX_FALLBACK_THRESHOLD", "10"))
PREFIX_MIN_TOKEN_LEN = 5

# ─── pg_trgm fallback over channel names ──────────────────────────────────────
# Similarity thresholds (tuned on real channel names, see search_channels step 3).
TRGM_TITLE_WS = float(os.getenv("TRGM_TITLE_WS", "0.45"))
TRGM_USERNAME_SIM = float(os.getenv("TRGM_USERNAME_SIM", "0.3"))


def _has_prefixable_token(query: str) -> bool:
    """Cheap Python guard: does the query contain a word of ≥ PREFIX_MIN_TOKEN_LEN
    characters (a :* candidate)? If not, the fallback is pointless — skip the extra query."""
    return any(len(w) >= PREFIX_MIN_TOKEN_LEN for w in (query or "").split())


def _prefix_ts_query(query: str):
    """tsquery with ':*' on lexemes of ≥5 characters (pseudo-stemming for agglutinative
    languages).

    Built FROM plainto_tsquery (same normalization/escaping as the exact path — the
    'russian' config stays symmetric): the text form of the tsquery
    ("'lexeme1' & 'lexeme2'") gets ':*' appended to long lexemes via a regex. Lexemes
    containing an escaped quote ('') do not match the pattern and stay exact (safe).
    NULLIF protects to_tsquery from an empty string (a query of stop words only: a
    ≥5-char stop word makes plainto return '' → without NULLIF this would be an ERROR;
    `vector @@ NULL` = NULL → simply no results)."""
    plain_text = func.cast(func.plainto_tsquery("russian", func.coalesce(query, "")), Text)
    prefixed = func.regexp_replace(plain_text, r"'([^']{5,})'", r"'\1':*", "g")
    return func.to_tsquery("russian", func.nullif(prefixed, ""))


def _prefix_fallback_applies(query: str, rows_len: int, limit: int, offset: int) -> bool:
    """Prefix-fallback decision: the page is underfilled by the exact path AND the exact
    total (= offset + rows_len, known precisely when rows_len < limit) ≤ threshold
    AND the query has a prefixable token."""
    return (
        rows_len < limit
        and (offset + rows_len) <= PREFIX_FALLBACK_THRESHOLD
        and _has_prefixable_token(query)
    )


def _escape_like(value: str) -> str:
    """
    Escape LIKE/ILIKE special characters (\\ % _) in user input.

    Without this, a query of `_` or `%` acted as a wildcard and matched every row.
    Used together with `.ilike(pattern, escape="\\")`.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _adult_filter(adult_only: bool):
    """
    WHERE condition for the 18+ toggle (safe search). TWO separate result modes,
    never mixed — the condition is ALWAYS applied:

      * normal (adult_only=False, DEFAULT) → `is_adult IS NOT TRUE` — everything EXCEPT 18+
        (keeps NULL=not determined and FALSE, hides only channels explicitly flagged 18+);
      * adult_only (adult_only=True)       → `is_adult IS TRUE`     — 18+ ONLY.

    For posts the filter applies to the post's channel (join to channels) — a post has
    no 18+ flag of its own, it inherits the channel's.
    """
    return Channel.is_adult.is_(True) if adult_only else Channel.is_adult.isnot(True)


# ─── Regional result filter (by channel section) ──────────────────────────────
# A channel's region/section is stored in channels.section (a tag from the CSV
# catalogue import). Channels whose section is in the list are EXCLUDED from bot
# search — both the channels themselves and their posts (the filter applies to the
# post's channel).
# NOTE — REVERSIBLE and with NO data deletion: this is ONLY a SELECT filter; rows stay
# in the DB, collection continues, and content reappears as soon as the filter is lifted.
# The section list is the HIDDEN_SECTIONS env var (comma-separated); empty (the
# default) = no filtering. To lift: HIDDEN_SECTIONS="" in the bot environment + restart.
# Coverage: only channels WITH a section tag are affected; channels without a section
# (NULL) are never filtered.
HIDDEN_SECTIONS = [
    s.strip() for s in os.getenv("HIDDEN_SECTIONS", "").split(",") if s.strip()
]


def _visible_channel_filter():
    """WHERE condition "channel is NOT in a hidden section" (regional result filter).

    Hides channels with ``channels.section`` ∈ HIDDEN_SECTIONS from results (both the
    channels and their posts — filtered by the post's channel). NULL sections and other
    sections stay visible. Empty list → ``TRUE`` (nothing hidden). Applied in ALL
    channel/post/media search paths and counters — consistently with _adult_filter."""
    if not HIDDEN_SECTIONS:
        return true()
    return or_(Channel.section.is_(None), Channel.section.notin_(HIDDEN_SECTIONS))


# ─── Result sorting: 3 toggle modes ───────────────────────────────────────────
# "r" — relevance (DEFAULT, ts_rank); "n" — new (by date); "v" — views.
# Every NEW query defaults to "r" (sort is kept in the callback, not in FSM).

def _channel_order(sort: str, ts_query):
    """ORDER BY for channels (tsvector path). For channels, "views" = subscribers.

    relevance: rank by the WEIGHTED search_vector — title=A(1.0), description=B(0.4),
    category/keywords=C(0.2) are baked into the tsvector itself (see the baseline migration). So
    `ts_rank(search_vector)` ITSELF yields title > description > keywords with no manual
    coefficient (previously "+0.3·keywords_vector", now formalized via setweight)."""
    if sort == "v":
        return [Channel.members_count.desc()]
    if sort == "n":
        return [Channel.created_at.desc().nullslast(), Channel.members_count.desc()]
    return [func.ts_rank(Channel.search_vector, ts_query).desc(), Channel.members_count.desc()]


def _channel_match(ts_query):
    """WHERE match for channels: by the WEIGHTED search_vector, which now includes
    title(A)/description(B)/category(C)/keywords(C) — topic-based channel search via
    keywords works through the same vector. The ILIKE fallback (below) also covers keywords."""
    return Channel.search_vector.op("@@")(ts_query)


def _channel_order_fallback(sort: str):
    """ORDER BY for channels (ILIKE fallback, no ts_rank)."""
    if sort == "n":
        return [Channel.created_at.desc().nullslast(), Channel.members_count.desc()]
    return [Channel.members_count.desc()]   # relevance≈popularity and views → subscribers


def _post_order(sort: str, ts_query):
    """ORDER BY for posts (tsvector path)."""
    if sort == "n":
        return [Post.post_date.desc().nullslast(), Post.views_count.desc()]
    if sort == "v":
        return [Post.views_count.desc(), Post.post_date.desc().nullslast()]
    return [func.ts_rank(Post.search_vector, ts_query).desc(), Post.views_count.desc()]


def _post_order_fallback(sort: str):
    """ORDER BY for posts (ILIKE fallback / media, no ts_rank)."""
    if sort == "n":
        return [Post.post_date.desc().nullslast()]
    return [Post.views_count.desc()]


# ─── Exact duplicates to the END of results ───────────────────────────────────
# What matters is POST uniqueness, not channel diversity. Duplicates are NOT deleted
# or capped (there is NO per-channel quota) — EXACT copies are pushed to the end. The
# first (best by active sort) occurrence stays in place; copies go to the tail.

def _dup_partition():
    """Grouping key for EXACT post duplicates. Identical NORMALIZED text (lower+trim)
    of sufficient length (≥20 chars) → one group (md5) — copy-paste/reposts (including
    across channels, common for promo posts). Short/empty text is NOT collapsed (risk of
    merging DIFFERENT posts) → the group is unique by id. ONLY exact text matches, not
    similar ones."""
    norm = func.lower(func.trim(func.coalesce(Post.text, "")))
    return case(
        (func.length(norm) >= 20, func.concat("t:", func.md5(norm))),
        else_=func.concat("u:", func.cast(Post.id, String)),
    )


def _dedup_order(order_cols):
    """ORDER BY that pushes EXACT duplicates to the END. Within a duplicate group,
    row_number() by the active sort: rn=1 (best ranked) stays in place, rn>1 (copies)
    go to the end. Unique posts keep their positions. The ordering is DETERMINISTIC
    (stable pagination, no jumps). NOTHING is removed — the count does not change,
    duplicates just sit lower. Applied ON TOP of the active sort (order_cols:
    relevance/views/date)."""
    dup_rn = func.row_number().over(
        partition_by=_dup_partition(),
        order_by=[*order_cols, Post.id.asc()],
    )
    # (dup_rn > 1): False(0)=original/unique — first; True(1)=copy — to the tail.
    return [(dup_rn > 1).asc(), *order_cols, Post.id.asc()]


async def _capped_dedup_posts(
    session: AsyncSession,
    match_condition,
    order_cols,
    adult_only: bool,
    limit: int,
    offset: int,
    cap: Optional[int] = None,
) -> List[Post]:
    """Post search with a MAX_RESULTS result cap and HIDING of exact duplicates
    (an earlier version pushed them to the tail — now they are hidden entirely).

    Step 1 (capped): top-MAX_RESULTS post ids by the ACTIVE sort — a fast parallel
    top-N over GIN (no window → no slowdown on popular words). Keeps Postgres load low.
    Step 2 (ranked): the same dedup window (row_number over md5(lower(trim(text))) ≥20
    chars, ordered by the active sort), but KEEP rn=1 (WHERE) instead of sorting copies
    to the tail → exact duplicates are HIDDEN (only the FIRST/best by sort is shown).
    Step 3: a page of UNIQUE posts ordered by the active sort.

    Weighting/sorting/keywords are unaffected. Short/empty text (<20) is NOT grouped
    (each is unique, see _dup_partition). has_more (N+1) is computed over UNIQUE posts:
    the caller requests limit=per_page+1. Hiding applies within the top-MAX_RESULTS
    candidates (window over top-N; WHERE rn=1 instead of ORDER BY rn>1)."""
    capped = (
        select(Post.id)
        .join(Channel, Channel.id == Post.channel_id)
        .where(match_condition)
        .where(_adult_filter(adult_only))
        .where(_visible_channel_filter())  # regional filter — before the dedup window
        .order_by(*order_cols, Post.id.asc())
        .limit(MAX_RESULTS if cap is None else cap)   # cap: the fallback's own ceiling
    ).subquery("capped")

    # Window over top-N: rn within an exact-duplicate group (best by sort = rn=1).
    dup_rn = func.row_number().over(
        partition_by=_dup_partition(),
        order_by=[*order_cols, Post.id.asc()],
    )
    ranked = (
        select(Post.id.label("pid"), dup_rn.label("rn"))
        .join(capped, capped.c.id == Post.id)
    ).subquery("ranked")

    stmt = (
        select(Post)
        .options(selectinload(Post.channel))
        .join(ranked, and_(ranked.c.pid == Post.id, ranked.c.rn == 1))  # HIDE copies
        .order_by(*order_cols, Post.id.asc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ─────────────────────────────────────────────
# Channel / group search
# ─────────────────────────────────────────────

async def search_channels(
    session: AsyncSession,
    query: str,
    limit: int = 10,
    offset: int = 0,
    channel_type: Optional[ChannelType] = None,
    adult_only: bool = False,
    sort: str = "r",
) -> List[Channel]:
    """
    Channel/group search: tsvector first, ILIKE if that returns nothing.
    Both paths support the channel_type filter.

    adult_only — 18+ toggle mode (see _adult_filter): False (default) → everything
    except 18+; True → 18+ only. The filter is ALWAYS applied (separate modes, no mixing).
    sort — sort mode: "r" relevance (default) / "n" new / "v" views.
    """
    # 1. Full-text search (tsvector): title/description OR topic keywords.
    ts_query = _safe_ts_query(query)
    stmt = (
        select(Channel)
        .where(Channel.is_active.is_(True))
        .where(_channel_match(ts_query))
        .where(_adult_filter(adult_only))
        .where(_visible_channel_filter())  # regional filter
        .order_by(*_channel_order(sort, ts_query))
        .limit(limit)
        .offset(offset)
    )
    if channel_type is not None:
        stmt = stmt.where(Channel.type == channel_type)

    result = await session.execute(stmt)
    rows = list(result.scalars().all())

    # 1a. Prefix fallback: few exact hits → the same tsvector path with lexeme:*
    # (word forms of agglutinative languages). The set ⊇ exact — exact hits are kept.
    if _prefix_fallback_applies(query, len(rows), limit, offset):
        pq = _prefix_ts_query(query)
        stmt = (
            select(Channel)
            .where(Channel.is_active.is_(True))
            .where(Channel.search_vector.op("@@")(pq))
            .where(_adult_filter(adult_only))
            .where(_visible_channel_filter())  # regional filter
            .order_by(*_channel_order(sort, pq))
            .limit(limit)
            .offset(offset)
        )
        if channel_type is not None:
            stmt = stmt.where(Channel.type == channel_type)
        prefix_rows = list((await session.execute(stmt)).scalars().all())
        if len(prefix_rows) > len(rows):
            return prefix_rows

    if rows:
        return rows

    # 2. Fallback: ILIKE over title / description / category / keywords
    pattern = f"%{_escape_like(query)}%"
    stmt = (
        select(Channel)
        .where(Channel.is_active.is_(True))
        .where(_adult_filter(adult_only))
        .where(_visible_channel_filter())  # regional filter
        .where(
            or_(
                Channel.title.ilike(pattern, escape="\\"),
                Channel.description.ilike(pattern, escape="\\"),
                Channel.category.ilike(pattern, escape="\\"),
                Channel.keywords.ilike(pattern, escape="\\"),
            )
        )
        .order_by(*_channel_order_fallback(sort))
        .limit(limit)
        .offset(offset)
    )
    if channel_type is not None:
        stmt = stmt.where(Channel.type == channel_type)

    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    if rows:
        return rows

    # 3. pg_trgm fallback over TITLE/username.
    # LAST step — triggers only where the result used to be "nothing found", so the
    # behaviour/ranking of the earlier steps is unchanged.
    # Language-independent (trigrams, not morphology): Latin-script languages, usernames
    # (always Latin), typos ("teleram" → Telegram…, ws=0.55 on real names). Only SHORT
    # strings (title/username), NOT post bodies.
    # Thresholds tuned on real data: word_similarity(query→title) ≥0.45 catches typos
    # without pulling in noise; similarity on username ≥0.3.
    # Functions instead of the %/<% operators: thresholds do not depend on session GUCs;
    # on thousands to tens of thousands of channels a similarity scan takes tens of ms —
    # acceptable for the last step. Beyond ~100k channels, switch to the operators
    # (which use the GIN gin_trgm_ops index, see the baseline migration).
    title_ws = func.word_similarity(query, Channel.title)
    uname_sim = func.similarity(func.coalesce(Channel.username, ""), query)
    trgm_rank = func.greatest(title_ws, uname_sim)
    stmt = (
        select(Channel)
        .where(Channel.is_active.is_(True))
        .where(_adult_filter(adult_only))
        .where(_visible_channel_filter())  # regional filter
        .where(or_(title_ws >= TRGM_TITLE_WS, uname_sim >= TRGM_USERNAME_SIM))
        .order_by(trgm_rank.desc(), Channel.members_count.desc())
        .limit(limit)
        .offset(offset)
    )
    if channel_type is not None:
        stmt = stmt.where(Channel.type == channel_type)

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def search_channels_by_text(
    session: AsyncSession,
    query: str,
    limit: int = 10,
    offset: int = 0,
    adult_only: bool = False,
    sort: str = "r",
) -> List[Channel]:
    """Search across all channels and groups without a type filter."""
    return await search_channels(
        session, query, limit=limit, offset=offset, adult_only=adult_only, sort=sort
    )


# ─────────────────────────────────────────────
# Post search
# ─────────────────────────────────────────────

async def search_posts(
    session: AsyncSession,
    query: str,
    limit: int = 10,
    offset: int = 0,
    sort: str = "r",
    adult_only: bool = False,
) -> List[Post]:
    """
    Post search: tsvector, then an ILIKE fallback.
    sort: "r" relevance (default, ts_rank) / "n" new (by date) / "v" views.

    adult_only — 18+ toggle mode: the filter applies to the post's CHANNEL (join to
    channels), since posts have no 18+ flag of their own. ALWAYS applied (see _adult_filter).
    """
    ts_query = _safe_ts_query(query)
    order_cols = _post_order(sort, ts_query)

    # tsvector path with the MAX_RESULTS cap + dedup over top-N
    rows = await _capped_dedup_posts(
        session, Post.search_vector.op("@@")(ts_query), order_cols, adult_only, limit, offset
    )

    # Prefix fallback: few exact hits → the same path with lexeme:*.
    # Prefix set ⊇ exact → exact hits are not lost; the prefix results are used only
    # if they actually found more (otherwise keep the original ranking).
    if _prefix_fallback_applies(query, len(rows), limit, offset):
        pq = _prefix_ts_query(query)
        prefix_rows = await _capped_dedup_posts(
            session, Post.search_vector.op("@@")(pq), _post_order(sort, pq),
            adult_only, limit, offset,
        )
        if len(prefix_rows) > len(rows):
            return prefix_rows

    if rows:
        return rows

    # ILIKE fallback under a HARD time budget (unbounded, it took tens of seconds).
    return await _bounded_ilike_posts(
        session, query, _post_order_fallback(sort), adult_only, limit, offset
    )


async def _bounded_ilike_posts(
    session: AsyncSession,
    query: str,
    order_cols,
    adult_only: bool,
    limit: int,
    offset: int,
) -> List[Post]:
    """ILIKE fallback over posts under a time budget.

    Why this approach (rather than "remove it" or "pg_trgm on posts"):
      * REMOVING IT — would lose rare but real hits where tsvector cannot help
        (substrings inside words/compounds, hashtags, languages without a stemmer);
      * pg_trgm ON POSTS — evaluated and rejected: GIN trigrams over the text of
        millions of posts would produce a multi-GB index and hours of build time
        (which is why trigrams are used only on SHORT channel fields);
      * A BUDGET — keeps cheap hits (the plan over the sort index stops early on dense
        matches) and cuts off expensive scans.

    Mechanics: `SET LOCAL statement_timeout` inside a SAVEPOINT. On timeout Postgres
    cancels the statement, rolling back to the savepoint heals the transaction, and an
    empty list ("nothing found") is returned — the bot replies normally. `SET LOCAL`
    survives RELEASE SAVEPOINT, so the budget is lifted in finally — the rest of the
    transaction (search_log write etc.) must not run under this timeout.
    """
    if not POST_ILIKE_FALLBACK:
        return []

    pattern = f"%{_escape_like(query)}%"
    try:
        async with session.begin_nested():   # SAVEPOINT — a timeout does not break the transaction
            await session.execute(
                text(f"SET LOCAL statement_timeout = {int(POST_FALLBACK_TIMEOUT_MS)}")
            )
            return await _capped_dedup_posts(
                session,
                Post.text.ilike(pattern, escape="\\"),
                order_cols,
                adult_only,
                limit,
                offset,
                cap=POST_FALLBACK_MAX_RESULTS,
            )
    except DBAPIError:
        # Budget exhausted (QueryCanceled) or another query failure — honestly "empty".
        # The query text is NOT logged (it may be blocked/sensitive).
        logger.info(
            "Post ILIKE fallback cut off by the %d ms budget — returning empty results",
            POST_FALLBACK_TIMEOUT_MS,
        )
        return []
    finally:
        # Lift the budget for the rest of the transaction (SET LOCAL lasts until its end).
        try:
            await session.execute(text("SET LOCAL statement_timeout = 0"))
        except Exception:   # transaction already unusable — do not disturb the main flow
            pass


# ─────────────────────────────────────────────
# Media search
# ─────────────────────────────────────────────

async def search_posts_with_media(
    session: AsyncSession,
    query: str,
    media_type: MediaType,
    limit: int = 10,
    offset: int = 0,
    adult_only: bool = False,
    sort: str = "r",
) -> List[Post]:
    """
    Search for posts that have an attached media file of the given type.
    Matches the post text and the media caption (ILIKE).

    adult_only — 18+ toggle mode (filter by the post's channel).
    sort: "n" — by date; otherwise (relevance/views) — by views
    (media results have no tsvector rank, relevance≈popularity).
    """
    # Text is matched via GIN search_vector @@ tsquery (by WORDS, 'russian' morphology —
    # like the main search), NOT via text ILIKE '%substring%' (unindexed ILIKE took tens
    # of seconds on popular words — scanning hundreds of thousands of media posts).
    # + EXISTS(media of the requested type). Captions are covered via post.text (Telethon
    # puts the caption into the message text) for most posts; media-only posts without
    # text are a known trade-off. Dedup/weighting/MAX_RESULTS cap are preserved via
    # _capped_dedup_posts.
    ts_query = _safe_ts_query(query)
    order_cols = _post_order(sort, ts_query)
    has_media = exists(
        select(Media.id).where(Media.post_id == Post.id, Media.type == media_type)
    )
    match = and_(Post.search_vector.op("@@")(ts_query), has_media)
    rows = await _capped_dedup_posts(session, match, order_cols, adult_only, limit, offset)

    # Prefix fallback — as in search_posts, with the same media condition.
    if _prefix_fallback_applies(query, len(rows), limit, offset):
        pq = _prefix_ts_query(query)
        prefix_match = and_(Post.search_vector.op("@@")(pq), has_media)
        prefix_rows = await _capped_dedup_posts(
            session, prefix_match, _post_order(sort, pq), adult_only, limit, offset
        )
        if len(prefix_rows) > len(rows):
            return prefix_rows
    return rows


# ─────────────────────────────────────────────
# Trending queries
# ─────────────────────────────────────────────

async def get_hot_searches(session: AsyncSession, limit: int = 10) -> List[HotSearch]:
    """Top trending queries by descending count."""
    stmt = select(HotSearch).order_by(HotSearch.count.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def increment_search_count(session: AsyncSession, query: str) -> None:
    """Upsert a trending query: create it or increment its counter."""
    normalized = query.strip().lower()
    if not normalized:
        return
    stmt = text("""
        INSERT INTO hot_searches (query, count, created_at, updated_at)
        VALUES (:query, 1, now(), now())
        ON CONFLICT (query)
        DO UPDATE SET count = hot_searches.count + 1, updated_at = now()
    """)
    await session.execute(stmt, {"query": normalized})


async def get_channel_by_id(session: AsyncSession, channel_id: int) -> Optional[Channel]:
    """Get a channel by its internal ID."""
    return await session.get(Channel, channel_id)


# ─────────────────────────────────────────────
# Analytics: users and query log
# ─────────────────────────────────────────────

async def upsert_user_stats(
    session: AsyncSession,
    telegram_user_id: int,
    username: Optional[str] = None,
    detected_lang: Optional[str] = None,
) -> Optional[str]:
    """
    Record a user interaction (atomic upsert) + resolve the interface language.

    New user → INSERT (requests_count=1, first/last_seen=now(),
    language=detected_lang — auto-detected from Telegram language_code).
    Existing user → last_seen=now(), requests_count += 1, username is updated
    (if non-empty). The language is NOT overwritten: ``COALESCE(current, detected)`` —
    once detected/chosen, the language is kept per user; the Language button changes
    it separately (set_user_language). Minimal personal data: id + username + activity + language.

    Returns:
        The user's effective language (via RETURNING) — for immediate rendering.
    """
    stmt = text("""
        INSERT INTO user_stats (telegram_user_id, username, requests_count, language, first_seen, last_seen)
        VALUES (:uid, :uname, 1, :lang, now(), now())
        ON CONFLICT (telegram_user_id)
        DO UPDATE SET
            requests_count = user_stats.requests_count + 1,
            last_seen = now(),
            username = COALESCE(EXCLUDED.username, user_stats.username),
            language = COALESCE(user_stats.language, EXCLUDED.language)
        RETURNING language
    """)
    result = await session.execute(
        stmt, {"uid": telegram_user_id, "uname": username, "lang": detected_lang}
    )
    return result.scalar_one_or_none()


async def set_user_language(
    session: AsyncSession,
    telegram_user_id: int,
    lang: str,
) -> None:
    """
    Explicitly save the user's interface language (Language button).

    Overwrites the current language with the chosen one. The user_stats row normally
    exists by now (created by UserStatsMiddleware), but an upsert is used just in case.
    """
    stmt = text("""
        INSERT INTO user_stats (telegram_user_id, requests_count, language, first_seen, last_seen)
        VALUES (:uid, 0, :lang, now(), now())
        ON CONFLICT (telegram_user_id)
        DO UPDATE SET language = EXCLUDED.language
    """)
    await session.execute(stmt, {"uid": telegram_user_id, "lang": lang})


async def record_search_log(
    session: AsyncSession,
    telegram_user_id: Optional[int],
    query: str,
    results_count: int,
    has_results: bool,
    adult_only: Optional[bool] = None,
) -> None:
    """
    Record a search with its outcome.

    has_results=False (nothing found) is the most valuable: it shows a gap in the index.
    The query text is normalized (strip+lower), as in hot_searches, so that top lists
    group consistently. Empty queries are not recorded.

    adult_only — whether 18+ mode was on during the search (for the demand breakdown
    in the panel). Additive; with None the column stays NULL (previous behaviour).
    """
    normalized = (query or "").strip().lower()[:100]
    if not normalized:
        return
    stmt = text("""
        INSERT INTO search_log (telegram_user_id, query, results_count, has_results, adult_only, created_at)
        VALUES (:uid, :q, :rc, :hr, :adult, now())
    """)
    await session.execute(
        stmt,
        {
            "uid": telegram_user_id,
            "q": normalized,
            "rc": int(results_count),
            "hr": bool(has_results),
            "adult": adult_only,
        },
    )


# ─────────────────────────────────────────────
# Result interaction telemetry
# ─────────────────────────────────────────────

async def log_interaction(
    session: AsyncSession,
    telegram_user_id: int,
    action: str,
    *,
    content_type: Optional[str] = None,
    sort: Optional[str] = None,
    page: Optional[int] = None,
    adult_only: Optional[bool] = None,
    is_last_page: Optional[bool] = None,
) -> None:
    """One INSERT per action — no joins, no counters, no read-before-write.

    Written AFTER replying to the user and in a SEPARATE short session (see the calls
    in handlers/search.py): telemetry must not live inside the search transaction.
    The table is append-only with no row conflicts, so the write takes no blocking locks.

    The query text is NOT stored: it is already in search_log and not needed here —
    which removes both the privacy concern and the extra bytes.
    """
    stmt = text("""
        INSERT INTO interaction_log
            (telegram_user_id, action, content_type, sort, page, adult_only, is_last_page)
        VALUES (:uid, :action, :ct, :sort, :page, :adult, :last)
    """)
    await session.execute(stmt, {
        "uid": telegram_user_id, "action": action[:12], "ct": content_type,
        "sort": sort, "page": page, "adult": adult_only, "last": is_last_page,
    })


# ─────────────────────────────────────────────
# Limit settings: stored in the DB, applied without a restart
# ─────────────────────────────────────────────

async def load_settings(session: AsyncSession) -> dict:
    """All limit parameters in one query (the table has only a handful of rows)."""
    rows = (await session.execute(text("SELECT key, value FROM app_settings"))).all()
    return {str(k): str(v) for k, v in rows}


async def is_premium(session: AsyncSession, telegram_user_id: int) -> bool:
    """Whether the user has an ACTIVE subscription right now.

    Active = status='active' AND (no expiry OR not yet expired). Expired rows are
    neither deleted nor rewritten by a cron job — expiry is computed by the query, so
    there is no window where a background task has not caught up yet.
    Index (telegram_user_id, status, expires_at) → a single row.
    """
    stmt = text("""
        SELECT 1 FROM user_subscription
        WHERE telegram_user_id = :uid AND status = 'active'
          AND (expires_at IS NULL OR expires_at > now())
        LIMIT 1
    """)
    return (await session.execute(stmt, {"uid": telegram_user_id})).first() is not None


# ─────────────────────────────────────────────
# Search sessions: only the LATEST result list is active
# ─────────────────────────────────────────────

async def start_search_session(
    session: AsyncSession,
    telegram_user_id: int,
    chat_id: int,
    message_id: int,
) -> None:
    """Register a new result list as the user's ACTIVE session.

    The previous session is deactivated simply by a newer row appearing (the active one
    is the latest by ``created_at``) — no separate "deactivate" step is needed, so there
    are no races between two quick queries: the later row wins.

    ``ON CONFLICT DO NOTHING`` — idempotency: re-registering the same message (a retry)
    neither adds rows nor shifts the session time.
    """
    stmt = text("""
        INSERT INTO search_session (telegram_user_id, chat_id, message_id, created_at)
        VALUES (:uid, :chat, :msg, now())
        ON CONFLICT (chat_id, message_id) DO NOTHING
    """)
    await session.execute(
        stmt, {"uid": telegram_user_id, "chat": chat_id, "msg": message_id}
    )


async def get_active_session_message(
    session: AsyncSession,
    telegram_user_id: int,
) -> Optional[tuple]:
    """(chat_id, message_id) of the user's ACTIVE (latest) session, or None.

    Index lookup on (telegram_user_id, created_at) → one row, cheap on the hot path
    (checked on every button press under a result list)."""
    stmt = text("""
        SELECT chat_id, message_id FROM search_session
        WHERE telegram_user_id = :uid
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    """)
    row = (await session.execute(stmt, {"uid": telegram_user_id})).first()
    return (int(row[0]), int(row[1])) if row else None


async def is_active_session(
    session: AsyncSession,
    telegram_user_id: int,
    chat_id: int,
    message_id: int,
) -> bool:
    """Whether the message is the user's ACTIVE result list.

    The (chat_id, message_id) pair comes from Telegram in the callback update, not from
    ``callback_data`` → it cannot be forged or "replayed" with a stale payload.
    No session (a message from before sessions existed, or after cleanup) = NOT active:
    the user is prompted to send a new query, which creates a session.
    """
    active = await get_active_session_message(session, telegram_user_id)
    return active is not None and active == (int(chat_id), int(message_id))


async def seconds_since_last_feedback(
    session: AsyncSession,
    telegram_user_id: int,
) -> Optional[float]:
    """
    Seconds since the user's last feedback message (rate limit 1/hour).
    None = no messages yet (the limit does not apply).

    Light query: index ix_feedback_user_created (uid, created_at) → one row.
    """
    stmt = text("""
        SELECT EXTRACT(EPOCH FROM (now() - max(created_at)))
        FROM feedback WHERE telegram_user_id = :uid
    """)
    result = await session.execute(stmt, {"uid": telegram_user_id})
    seconds = result.scalar_one_or_none()
    return float(seconds) if seconds is not None else None


async def add_feedback(
    session: AsyncSession,
    telegram_user_id: Optional[int],
    feedback_text: str,
    language: Optional[str] = None,
) -> None:
    """
    Save a user feedback message.

    telegram_user_id is ONLY for internal moderation (panel) and rate limiting;
    it is never shown to the user in the bot (the anonymity promise holds).
    """
    stmt = text("""
        INSERT INTO feedback (telegram_user_id, text, language, created_at)
        VALUES (:uid, :txt, :lang, now())
    """)
    await session.execute(
        stmt, {"uid": telegram_user_id, "txt": feedback_text, "lang": language}
    )


async def get_analytics_summary(session: AsyncSession) -> dict:
    """
    Summary for /stats: users, activity, query volume.

    Returns a dict: total_users, active_7d, total_requests, total_searches,
    empty_searches.
    """
    total_users = (
        await session.execute(select(func.count()).select_from(UserStats))
    ).scalar_one() or 0

    active_7d = (
        await session.execute(
            select(func.count())
            .select_from(UserStats)
            .where(UserStats.last_seen >= func.now() - text("interval '7 days'"))
        )
    ).scalar_one() or 0

    total_requests = (
        await session.execute(select(func.coalesce(func.sum(UserStats.requests_count), 0)))
    ).scalar_one() or 0

    total_searches = (
        await session.execute(select(func.count()).select_from(SearchLog))
    ).scalar_one() or 0

    empty_searches = (
        await session.execute(
            select(func.count())
            .select_from(SearchLog)
            .where(SearchLog.has_results.is_(False))
        )
    ).scalar_one() or 0

    return {
        "total_users": total_users,
        "active_7d": active_7d,
        "total_requests": total_requests,
        "total_searches": total_searches,
        "empty_searches": empty_searches,
    }


async def get_top_search_queries(
    session: AsyncSession,
    limit: int = 10,
    empty_only: bool = False,
) -> List[tuple]:
    """
    Top queries from search_log by frequency.

    empty_only=True → only queries with has_results=false (gaps in the index —
    what to collect next). Returns a list of (query, count) tuples.
    """
    stmt = (
        select(SearchLog.query, func.count().label("cnt"))
        .group_by(SearchLog.query)
        .order_by(func.count().desc(), SearchLog.query)
        .limit(limit)
    )
    if empty_only:
        stmt = stmt.where(SearchLog.has_results.is_(False))
    result = await session.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]
