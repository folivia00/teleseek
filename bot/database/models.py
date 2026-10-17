"""
SQLAlchemy database models for TeleSeek.
Models: Channel, Post, Media, HotSearch, UserStats, SearchLog, Feedback.
"""

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed as sa_Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all models."""
    pass


class ChannelType(str, enum.Enum):
    """Kind of Telegram resource."""
    channel = "channel"
    group = "group"
    supergroup = "supergroup"


class MediaType(str, enum.Enum):
    """Kind of media file."""
    photo = "photo"
    video = "video"
    audio = "audio"
    document = "document"


class ParseStatus(str, enum.Enum):
    """
    Channel status in the cyclic collection queue (the "round" mechanism).

    - new         — never collected yet (newest_parsed_id IS NULL);
    - in_progress — collected in rounds (fetch new posts + go deeper into history);
    - done_depth  — depth limit reached (200 posts) OR the channel history ended →
                    no more deepening, only new posts are fetched;
    - error       — the last attempt failed (see last_error);
    - restricted_skip — Telegram-restricted channel (restricted=True): its history is
                    NOT served via the API to a non-subscribed account.
                    Workers do NOT pick such channels, to avoid wasted passes.
    """
    new = "new"
    in_progress = "in_progress"
    done_depth = "done_depth"
    error = "error"
    restricted_skip = "restricted_skip"


class Channel(Base):
    """
    Telegram channel or group.
    The main search unit: matched by title, description and category.
    """
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, comment="Unique Telegram ID")
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="Channel/group title")
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="@username (if any)")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="Description")
    type: Mapped[ChannelType] = mapped_column(
        Enum(ChannelType, name="channel_type"),
        nullable=False,
        default=ChannelType.channel,
        comment="Type: channel/group/supergroup",
    )
    members_count: Mapped[int] = mapped_column(Integer, default=0, comment="Subscriber count")
    invite_link: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="Invite link")
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="Category (cooking, crypto, ...)")
    # Section from the source catalogue (CSV). Used for result filtering.
    section: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="Section from the source catalogue")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="Whether the channel is active")
    # 18+ flag (NULL = not determined).
    # NOTE: the collector does NOT fill this field — Telegram exposes no reliable 18+ signal.
    is_adult: Mapped[Optional[bool]] = mapped_column(
        Boolean,
        nullable=True,
        default=None,
        comment="18+ flag: NULL=not determined, true/false — set manually",
    )
    # Channel origin: 'dispatcher' — collected by the data pipeline (which revisits and
    # updates it); 'tgstat' — imported from an external source (the pipeline does NOT
    # collect it; restricted channels that Telegram hides from regular accounts).
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="dispatcher", server_default="dispatcher",
        comment="Origin: dispatcher | tgstat",
    )

    # Full-text search — tsvector over title + description
    search_vector: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        nullable=True,
        comment="Full-text index (tsvector)",
    )
    # Topic keywords derived from the channel's posts. Filled by a standalone
    # keyword-building script (ts_stat over posts). Channel search matches here too.
    keywords: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Topic keywords (space-separated stemmed lexemes)",
    )
    # keywords tsvector — GENERATED from keywords in the DB (baseline migration); read-only for the ORM.
    keywords_vector: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        sa_Computed("to_tsvector('russian', coalesce(keywords, ''))", persisted=True),
        nullable=True,
        comment="keywords tsvector (generated from keywords; for topic-based channel search)",
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    posts: Mapped[List["Post"]] = relationship("Post", back_populates="channel", cascade="all, delete-orphan")

    # Indexes
    __table_args__ = (
        Index("ix_channels_title", "title"),
        Index("ix_channels_category", "category"),
        Index("ix_channels_members_count", "members_count"),
        Index("ix_channels_search_vector", "search_vector", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        return f"<Channel id={self.id} title={self.title!r} type={self.type}>"


class Post(Base):
    """
    Post (message) from a Telegram channel.
    Searched by its text.
    """
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(Integer, ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="Telegram message ID")
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="Post text")
    views_count: Mapped[int] = mapped_column(BigInteger, default=0, comment="View count")
    forwards_count: Mapped[int] = mapped_column(BigInteger, default=0, comment="Forward count")
    post_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, comment="Publication date")
    # Post origin: 'dispatcher' (collected by the data pipeline) | 'tgstat' (external import).
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="dispatcher", server_default="dispatcher",
        comment="Post origin: dispatcher | tgstat",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Full-text search
    search_vector: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        nullable=True,
        comment="Full-text index (tsvector)",
    )

    # Relationships
    channel: Mapped["Channel"] = relationship("Channel", back_populates="posts")
    media: Mapped[List["Media"]] = relationship("Media", back_populates="post", cascade="all, delete-orphan")

    # Indexes and constraints
    __table_args__ = (
        # DB-level dedup: one channel post = one row.
        # Enables an atomic INSERT ... ON CONFLICT upsert and removes the
        # TOCTOU race during parallel collection.
        UniqueConstraint("channel_id", "telegram_message_id", name="uq_posts_channel_msg"),
        Index("ix_posts_channel_id", "channel_id"),
        Index("ix_posts_post_date", "post_date"),
        Index("ix_posts_views_count", "views_count"),
        Index("ix_posts_search_vector", "search_vector", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        return f"<Post id={self.id} channel_id={self.channel_id} msg_id={self.telegram_message_id}>"


class Media(Base):
    """
    Media file attached to a post.
    """
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(Integer, ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[MediaType] = mapped_column(
        Enum(MediaType, name="media_type"),
        nullable=False,
        comment="Media type: photo/video/audio/document",
    )
    file_id: Mapped[str] = mapped_column(String(512), nullable=False, comment="Telegram file_id")
    file_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="File name")
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="File size in bytes")
    duration: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="Duration in seconds (video/audio)")
    caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="Media caption")

    # Relationships
    post: Mapped["Post"] = relationship("Post", back_populates="media")

    # Indexes
    __table_args__ = (
        Index("ix_media_post_id", "post_id"),
        Index("ix_media_type", "type"),
    )

    def __repr__(self) -> str:
        return f"<Media id={self.id} type={self.type} post_id={self.post_id}>"


class HotSearch(Base):
    """
    Trending search queries.
    Originally shown below each result list.
    """
    __tablename__ = "hot_searches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, comment="Query text")
    count: Mapped[int] = mapped_column(Integer, default=1, comment="Search count")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Indexes
    __table_args__ = (
        Index("ix_hot_searches_count", "count"),
        Index("ix_hot_searches_query", "query"),
    )

    def __repr__(self) -> str:
        return f"<HotSearch query={self.query!r} count={self.count}>"


class ChannelQueue(Base):
    """
    Channel collection queue (the "round" mechanism).

    Source: addresses from a CSV catalogue — username + title + category/section.
    Channel metadata (telegram_id, description, subscribers) is NOT stored on import —
    it is fetched on the FIRST collection pass and written to ``channels``; the queue
    row links to the real channel via ``channel_id`` (filled after the first pass).

    The "round" state (newest/oldest_parsed_id, parse_status, …) lives here too.
    These fields sit on the queue rather than on ``channels`` because a CSV channel has
    no ``telegram_id`` before its first pass (and ``channels.telegram_id`` is NOT NULL
    UNIQUE), i.e. no ``channels`` row exists yet for an uncollected channel.

    Import dedup is by ``username`` (UNIQUE).
    """
    __tablename__ = "channel_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Channel address (dedup key). Normalized username without the leading @.
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="@username from CSV (dedup key)")
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="Title from CSV (before the first pass)")
    url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="Original URL from CSV")
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="Category from CSV")
    section: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="Section from CSV")

    # Link to the real channel (after the first successful metadata fetch).
    channel_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("channels.id", ondelete="SET NULL"), nullable=True,
        comment="FK to channels.id (filled on the first pass)",
    )

    # ─── Dispatcher (multi-account) ──────────────────────────────────────────
    # Pins a channel to a worker account: each channel is processed by ONLY one
    # account (no double collection). Assigned by the coordinating dispatcher
    # (assign_channels). NULL = not pinned yet. Each worker selects strictly
    # WHERE assigned_account = <its telegram id> — partitions never overlap.
    assigned_account: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True,
        comment="Telegram id of the pinned worker account (dispatcher); NULL=not pinned",
    )

    # Result of the public web-preview probe on import:
    # 'with_posts' (web-accessible, metadata materialized) | 'no_preview' |
    # 'not_found' | 'error'; NULL = not probed (older rows). This is a forecast,
    # NOT a verdict: the dispatcher still visits ALL channels. The external-import
    # candidate pool = WHERE web_status IN ('no_preview','not_found')
    # [+ later parse_status='restricted_skip'].
    web_status: Mapped[Optional[str]] = mapped_column(
        String(16), nullable=True,
        comment="Web-probe result on import: with_posts/no_preview/not_found/error; NULL=not probed",
    )

    # Worker pick priority: higher goes first. A worker orders its partition by
    # (priority DESC, last_parsed_at ASC NULLS FIRST). Used for a ONE-OFF boost
    # (e.g. quickly collecting content to test the content filter): after the first
    # successful pass the worker resets priority to 0, so the boosted set does not
    # starve the rest of the queue indefinitely.
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
        comment="Worker pick priority (higher goes first); 0=normal",
    )

    # ─── "Round" state ───────────────────────────────────────────────────────
    # ID of the newest processed post (for fetching new ones: min_id=newest).
    newest_parsed_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="Newest processed msg_id")
    # ID of the oldest processed post (for going deeper: max_id=oldest).
    oldest_parsed_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="Oldest processed msg_id")
    # Number of posts already saved for this channel (depth counter, limit 200).
    posts_parsed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="Posts collected so far (depth limit)")
    parse_status: Mapped[ParseStatus] = mapped_column(
        Enum(ParseStatus, name="parse_status"),
        nullable=False,
        default=ParseStatus.new,
        comment="new / in_progress / done_depth / error",
    )
    last_parsed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, comment="When it was last visited")
    last_error: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="Last error text (if parse_status=error)")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_channel_queue_parse_status", "parse_status"),
        Index("ix_channel_queue_category", "category"),
        Index("ix_channel_queue_last_parsed_at", "last_parsed_at"),
        # Dispatcher: each worker selects its partition by this column on every pass.
        Index("ix_channel_queue_assigned_account", "assigned_account"),
        # Priority is the first sort key when a worker picks a channel.
        Index("ix_channel_queue_priority", "priority"),
    )

    def __repr__(self) -> str:
        return f"<ChannelQueue username={self.username!r} status={self.parse_status} count={self.posts_parsed_count}>"


class ChannelAccess(Base):
    """
    Channel access cache (channel_id + access_hash), bound to an ACCOUNT.

    Why: repeated ``client.get_entity(username)`` calls hit Telegram's daily
    ResolveUsername limit (the main collection bottleneck). Solution: resolve a channel
    by username once, store (telegram_id, raw channel_id, access_hash), and from then on
    use ``InputPeerChannel(channel_id, access_hash)`` WITHOUT resolving.

    Bound to an account (``account_id`` = telegram id of the collecting account): the
    access_hash DIFFERS per account. The table is multi-account from the start — a
    channel has a separate row (with its own hash) for each dispatcher account.

    Fields:
      * account_id        — telegram id of the collecting account (owner of the access_hash);
      * telegram_id       — canonical channel id in bot-API form (-100…), == channels.telegram_id;
      * access_channel_id — "raw" entity.id (positive) for InputPeerChannel;
      * access_hash       — channel access_hash for this account;
      * username          — informational (as of resolve time; may change).
    """
    __tablename__ = "channel_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="Telegram id of the collecting account")
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="Canonical channel id (-100…, == channels.telegram_id)")
    access_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="Raw entity.id for InputPeerChannel")
    access_hash: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="Channel access_hash for this account")
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="@username at resolve time (informational)")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        # One channel → one row per account (multi-account).
        UniqueConstraint("account_id", "telegram_id", name="uq_channel_access_account_channel"),
        Index("ix_channel_access_lookup", "account_id", "telegram_id"),
    )

    def __repr__(self) -> str:
        return f"<ChannelAccess account={self.account_id} tg_id={self.telegram_id}>"


class UserStats(Base):
    """
    Minimal bot user statistics (audience analytics).

    Stores ONLY activity aggregates — NO personal data (names, phone numbers,
    message text): telegram_user_id + timestamps + a request counter.
    ``username`` is optional and informational (may be NULL and change over time);
    it only makes the /stats summary readable, nothing is filtered by it.

    Written by UserStatsMiddleware on EVERY user interaction (any message/callback):
    new user → INSERT (requests_count=1), existing → atomic upsert
    (last_seen=now(), requests_count += 1). See queries.upsert_user_stats.
    """
    __tablename__ = "user_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, comment="User Telegram id (dedup key)"
    )
    username: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="@username at the last interaction (informational)"
    )
    # Bot interface language: 'en'/'ru'/… ; NULL = not determined yet.
    # Detected AUTOMATICALLY from Telegram language_code on first contact (fixed on
    # INSERT), then kept per user; the Language button changes it explicitly. Request
    # accounting (upsert) does NOT overwrite it — COALESCE keeps the chosen/detected one.
    language: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True,
        comment="Interface language (en/ru/…); NULL=not determined",
    )
    requests_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
        comment="How many times the user interacted with the bot",
    )
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="First interaction"
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="Last interaction"
    )

    __table_args__ = (
        # Activity over a period (last_seen >= now()-N) — the main summary filter.
        Index("ix_user_stats_last_seen", "last_seen"),
    )

    def __repr__(self) -> str:
        return f"<UserStats uid={self.telegram_user_id} reqs={self.requests_count}>"


class SearchLog(Base):
    """
    Search query log (demand analytics).

    One row = one user search. The key value is the ``has_results`` flag:
    queries with has_results=false (nothing found) show what is MISSING from the
    index → a direct hint about which topics/channels to collect for real demand.

    Minimal personal data: ``telegram_user_id`` is stored only as a Telegram id (to
    link activity), and the query text is normalized (strip+lower) — as in
    hot_searches. No retroactive data: counting starts at deployment.
    """
    __tablename__ = "search_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True, comment="Telegram id of the searching user"
    )
    query: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Normalized query text (strip+lower)"
    )
    results_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
        comment="Number of results found (channels+posts)",
    )
    has_results: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="Whether the result was non-empty (false=gap in the index)",
    )
    adult_only: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True,
        comment="Whether 18+ mode was on during the search; NULL=unknown (older rows)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="When the search happened"
    )

    __table_args__ = (
        # Grouping for top / empty queries and filtering by date.
        Index("ix_search_log_query", "query"),
        Index("ix_search_log_has_results", "has_results"),
        Index("ix_search_log_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<SearchLog uid={self.telegram_user_id} q={self.query!r} hits={self.results_count}>"


class Feedback(Base):
    """
    User feedback messages.

    One row = one message sent from the bot (Feedback button, FSM text collection).
    ANONYMITY: the id is NEVER shown to users in the bot; ``telegram_user_id`` is
    stored ONLY for internal moderation (localhost panel, Feedback section) and
    rate limiting (1 message per hour per user).

    ``is_favorite`` — a "favorite" mark toggled in the panel; ``language`` — the
    user's interface language at send time (informational, to understand the audience).
    """
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True,
        comment="Author Telegram id (internal: moderation + rate limit; NOT shown in the bot)",
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, comment="Message text")
    language: Mapped[Optional[str]] = mapped_column(
        String(8), nullable=True, comment="Author interface language at send time (en/ru/…)"
    )
    is_favorite: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
        comment="Favorite mark (panel toggle)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="When it was sent"
    )

    __table_args__ = (
        # Panel feed — newest first (ORDER BY created_at DESC LIMIT N).
        Index("ix_feedback_created_at", "created_at"),
        # Rate limit: the user's latest message (WHERE uid ORDER BY created_at DESC LIMIT 1).
        Index("ix_feedback_user_created", "telegram_user_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Feedback id={self.id} uid={self.telegram_user_id} fav={self.is_favorite}>"


class SearchSession(Base):
    """
    Search session = ONE result message.

    Why a table. The bot keeps the whole result state in button ``callback_data``
    (query + page + type + sort), so ANY old message stayed fully functional: a user
    sent a new query but could keep paging through the previous results. To make only
    the latest result list "active", a session marker is needed OUTSIDE callback_data —
    otherwise (a) it would have to be squeezed into Telegram's 64-byte limit, where the
    query is already truncated, and (b) it could be forged.

    The session id is the ``(chat_id, message_id)`` pair of the result message: Telegram
    itself supplies it in the callback update, so it cannot be forged. The active session
    is the user's row with the latest ``created_at``; a new query adds a row and thereby
    deactivates the previous one.

    Storing it in the DB (rather than FSM/MemoryStorage) is deliberate: FSM is reset on
    bot restart, so after every deployment even the latest result list would go stale
    for all users. Rows live until a daily cleanup that keeps one (the latest) per user.

    NOTE: this is NOT telemetry. Query analytics lives in ``search_log`` (the cleanup
    does NOT touch it).
    """
    __tablename__ = "search_session"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="Session owner (Telegram id)"
    )
    chat_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="Chat of the result message"
    )
    message_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="Result message id (session identifier)"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), comment="When the result list was sent"
    )

    __table_args__ = (
        # Hot path: "user's latest session" — WHERE uid ORDER BY created_at DESC LIMIT 1.
        Index("ix_search_session_user_created", "telegram_user_id", "created_at"),
        # One message = one session (guards against duplicates on resend).
        UniqueConstraint("chat_id", "message_id", name="uq_search_session_message"),
    )

    def __repr__(self) -> str:
        return f"<SearchSession uid={self.telegram_user_id} msg={self.message_id}>"
