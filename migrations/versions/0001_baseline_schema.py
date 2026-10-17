"""Baseline schema.

A single migration that creates the complete TeleSeek schema: channels, posts,
media, search analytics, user feedback, limits/premium infrastructure and the
tables used by the data-collection pipeline (channel_queue, channel_access).

Search design notes:
  * `channels.search_vector` and `posts.search_vector` are maintained by
    BEFORE INSERT/UPDATE triggers with weighted fields (setweight):
    channels — title=A, description=B, category/keywords=C;
    posts — lead (first 200 characters)=A, body=B.
    Because the vectors are trigger-built, rows written by any producer
    (the data pipeline, imports, seed scripts) are indexed automatically.
  * The 'russian' text-search configuration is used for both the index and
    the query: Russian words are stemmed by the Russian Snowball stemmer and
    Latin-script words by the English one; other languages match by exact form
    (the bot adds a prefix fallback for agglutinative languages).
  * pg_trgm GIN indexes on channel title/username provide typo-tolerant
    channel lookup.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-17
"""
import textwrap
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ─── Extensions ──────────────────────────────────────────────────────────────
_EXTENSIONS: list[str] = [
    """
    CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public
    """,
]

# ─── Enum types ──────────────────────────────────────────────────────────────
_TYPES: list[str] = [
    """
    CREATE TYPE public.channel_type AS ENUM (
        'channel',
        'group',
        'supergroup'
    )
    """,
    """
    CREATE TYPE public.media_type AS ENUM (
        'photo',
        'video',
        'audio',
        'document'
    )
    """,
    """
    CREATE TYPE public.parse_status AS ENUM (
        'new',
        'in_progress',
        'done_depth',
        'error',
        'restricted_skip'
    )
    """,
]

# ─── Trigger functions (search_vector is built by triggers, so new rows are indexed automatically) ───
_FUNCTIONS: list[str] = [
    """
    CREATE FUNCTION public.update_channel_search_vector() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
            BEGIN
                NEW.search_vector :=
                    setweight(to_tsvector('russian', coalesce(NEW.title, '')), 'A') ||
                    setweight(to_tsvector('russian', coalesce(NEW.description, '')), 'B') ||
                    setweight(to_tsvector('russian', coalesce(NEW.category, '')), 'C') ||
                    setweight(to_tsvector('russian', coalesce(NEW.keywords, '')), 'C');
                RETURN NEW;
            END;
            $$
    """,
    """
    CREATE FUNCTION public.update_post_search_vector() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
            BEGIN
                NEW.search_vector :=
                    setweight(to_tsvector('russian', left(coalesce(NEW.text, ''), 200)), 'A') ||
                    setweight(to_tsvector('russian', substr(coalesce(NEW.text, ''), 201)), 'B');
                RETURN NEW;
            END;
            $$
    """,
]

# ─── Tables, sequences and column comments ───────────────────────────────────
_TABLES: list[str] = [
    """
    CREATE TABLE public.admin_action_log (
        id bigint NOT NULL,
        actor character varying(64),
        action character varying(32) NOT NULL,
        target character varying(64),
        old_value character varying(128),
        new_value character varying(128),
        created_at timestamp with time zone DEFAULT now() NOT NULL
    )
    """,
    """
    COMMENT ON COLUMN public.admin_action_log.actor IS 'Who performed the action (panel / operator name)'
    """,
    """
    COMMENT ON COLUMN public.admin_action_log.action IS 'setting_update | premium_grant | premium_revoke'
    """,
    """
    COMMENT ON COLUMN public.admin_action_log.target IS 'Setting key or telegram_user_id'
    """,
    """
    CREATE SEQUENCE public.admin_action_log_id_seq
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.admin_action_log_id_seq OWNED BY public.admin_action_log.id
    """,
    """
    CREATE TABLE public.app_settings (
        key character varying(48) NOT NULL,
        value text NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        updated_by character varying(64)
    )
    """,
    """
    CREATE TABLE public.channel_access (
        id integer NOT NULL,
        account_id bigint NOT NULL,
        telegram_id bigint NOT NULL,
        access_channel_id bigint NOT NULL,
        access_hash bigint NOT NULL,
        username character varying(64),
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL
    )
    """,
    """
    COMMENT ON COLUMN public.channel_access.account_id IS 'Telegram id of the collector account'
    """,
    """
    COMMENT ON COLUMN public.channel_access.telegram_id IS 'Canonical channel id (-100…, == channels.telegram_id)'
    """,
    """
    COMMENT ON COLUMN public.channel_access.access_channel_id IS 'Raw entity.id for InputPeerChannel'
    """,
    """
    COMMENT ON COLUMN public.channel_access.access_hash IS 'Channel access_hash for this account'
    """,
    """
    COMMENT ON COLUMN public.channel_access.username IS '@username at resolve time (informational)'
    """,
    """
    CREATE SEQUENCE public.channel_access_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.channel_access_id_seq OWNED BY public.channel_access.id
    """,
    """
    CREATE TABLE public.channel_edit_log (
        id integer NOT NULL,
        channel_id integer NOT NULL,
        username character varying(64),
        field character varying(32) NOT NULL,
        old_value character varying(128),
        new_value character varying(128),
        source character varying(32) DEFAULT 'panel'::character varying NOT NULL,
        created_at timestamp with time zone DEFAULT now()
    )
    """,
    """
    COMMENT ON COLUMN public.channel_edit_log.channel_id IS 'channels.id of the edited channel (no FK cascade — the log outlives the channel)'
    """,
    """
    COMMENT ON COLUMN public.channel_edit_log.username IS 'Channel @username at edit time (informational)'
    """,
    """
    COMMENT ON COLUMN public.channel_edit_log.field IS 'Edited field: is_adult | category'
    """,
    """
    COMMENT ON COLUMN public.channel_edit_log.old_value IS 'Value BEFORE the edit (as text)'
    """,
    """
    COMMENT ON COLUMN public.channel_edit_log.new_value IS 'Value AFTER the edit (as text)'
    """,
    """
    COMMENT ON COLUMN public.channel_edit_log.source IS 'Edit source (panel)'
    """,
    """
    COMMENT ON COLUMN public.channel_edit_log.created_at IS 'When the edit was applied'
    """,
    """
    CREATE SEQUENCE public.channel_edit_log_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.channel_edit_log_id_seq OWNED BY public.channel_edit_log.id
    """,
    """
    CREATE TABLE public.channel_queue (
        id integer NOT NULL,
        username character varying(64) NOT NULL,
        name character varying(255),
        url character varying(255),
        category character varying(64),
        section character varying(64),
        channel_id integer,
        newest_parsed_id bigint,
        oldest_parsed_id bigint,
        posts_parsed_count integer DEFAULT 0 NOT NULL,
        parse_status public.parse_status DEFAULT 'new'::public.parse_status NOT NULL,
        last_parsed_at timestamp with time zone,
        last_error character varying(255),
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        assigned_account bigint,
        priority integer DEFAULT 0 NOT NULL,
        web_status character varying(16)
    )
    """,
    """
    COMMENT ON COLUMN public.channel_queue.username IS '@username from the import list (dedup key)'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.name IS 'Title from the import list (before the first ingestion)'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.url IS 'Original URL from the import list'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.category IS 'Category from the import list'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.section IS 'Section from the import list'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.channel_id IS 'FK to channels.id (set on first ingestion)'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.newest_parsed_id IS 'Newest processed msg_id'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.oldest_parsed_id IS 'Oldest processed msg_id'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.posts_parsed_count IS 'Posts accumulated (depth limit)'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.parse_status IS 'new / in_progress / done_depth / error'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.last_parsed_at IS 'When the channel was last processed'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.last_error IS 'Text of the last error (if parse_status=error)'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.assigned_account IS 'Telegram id of the collector worker the channel is pinned to; NULL = unassigned'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.priority IS 'Worker pick priority (higher = earlier); 0 = normal'
    """,
    """
    COMMENT ON COLUMN public.channel_queue.web_status IS 'Public web-preview check result at import: with_posts/no_preview/not_found/error; NULL = not checked'
    """,
    """
    CREATE SEQUENCE public.channel_queue_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.channel_queue_id_seq OWNED BY public.channel_queue.id
    """,
    """
    CREATE TABLE public.channels (
        id integer NOT NULL,
        telegram_id bigint NOT NULL,
        title character varying(255) NOT NULL,
        username character varying(64),
        description text,
        type public.channel_type NOT NULL,
        members_count integer DEFAULT 0 NOT NULL,
        invite_link character varying(255),
        category character varying(64),
        is_active boolean DEFAULT true NOT NULL,
        search_vector tsvector,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL,
        is_adult boolean,
        section character varying(64),
        source character varying(16) DEFAULT 'dispatcher'::character varying NOT NULL,
        keywords text,
        keywords_vector tsvector GENERATED ALWAYS AS (to_tsvector('russian'::regconfig, COALESCE(keywords, ''::text))) STORED
    )
    """,
    """
    COMMENT ON COLUMN public.channels.telegram_id IS 'Unique Telegram ID'
    """,
    """
    COMMENT ON COLUMN public.channels.title IS 'Channel/group title'
    """,
    """
    COMMENT ON COLUMN public.channels.username IS '@username (if any)'
    """,
    """
    COMMENT ON COLUMN public.channels.description IS 'Description'
    """,
    """
    COMMENT ON COLUMN public.channels.type IS 'Type: channel/group/supergroup'
    """,
    """
    COMMENT ON COLUMN public.channels.members_count IS 'Number of members/subscribers'
    """,
    """
    COMMENT ON COLUMN public.channels.invite_link IS 'Invite link'
    """,
    """
    COMMENT ON COLUMN public.channels.category IS 'Category'
    """,
    """
    COMMENT ON COLUMN public.channels.is_active IS 'Whether the channel is active'
    """,
    """
    COMMENT ON COLUMN public.channels.search_vector IS 'Full-text index (tsvector)'
    """,
    """
    COMMENT ON COLUMN public.channels.is_adult IS '18+ flag (content filter): NULL = undetermined, true/false = labelled'
    """,
    """
    COMMENT ON COLUMN public.channels.section IS 'Section/region from the source'
    """,
    """
    COMMENT ON COLUMN public.channels.source IS 'Origin: dispatcher (data pipeline) | tgstat (external import) | web | group'
    """,
    """
    COMMENT ON COLUMN public.channels.keywords IS 'Topic keywords derived from the channel''s posts (stemmed, space-separated)'
    """,
    """
    CREATE SEQUENCE public.channels_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.channels_id_seq OWNED BY public.channels.id
    """,
    """
    CREATE TABLE public.feedback (
        id integer NOT NULL,
        telegram_user_id bigint,
        text text NOT NULL,
        language character varying(8),
        is_favorite boolean DEFAULT false NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL
    )
    """,
    """
    COMMENT ON COLUMN public.feedback.telegram_user_id IS 'Author Telegram id (internal: moderation + rate limiting; never shown in the bot)'
    """,
    """
    COMMENT ON COLUMN public.feedback.text IS 'Feedback text'
    """,
    """
    COMMENT ON COLUMN public.feedback.language IS 'Author''s interface language at send time (en/ru/…)'
    """,
    """
    COMMENT ON COLUMN public.feedback.is_favorite IS 'Favourite mark (toggle in the panel)'
    """,
    """
    COMMENT ON COLUMN public.feedback.created_at IS 'When it was sent'
    """,
    """
    CREATE SEQUENCE public.feedback_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.feedback_id_seq OWNED BY public.feedback.id
    """,
    """
    CREATE TABLE public.hot_searches (
        id integer NOT NULL,
        query character varying(100) NOT NULL,
        count integer DEFAULT 1 NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL
    )
    """,
    """
    COMMENT ON COLUMN public.hot_searches.query IS 'Query text'
    """,
    """
    COMMENT ON COLUMN public.hot_searches.count IS 'Number of searches'
    """,
    """
    CREATE SEQUENCE public.hot_searches_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.hot_searches_id_seq OWNED BY public.hot_searches.id
    """,
    """
    CREATE TABLE public.interaction_log (
        id bigint NOT NULL,
        telegram_user_id bigint NOT NULL,
        action character varying(12) NOT NULL,
        content_type character varying(12),
        sort character varying(1),
        page smallint,
        adult_only boolean,
        is_last_page boolean,
        created_at timestamp with time zone DEFAULT now() NOT NULL
    )
    """,
    """
    COMMENT ON COLUMN public.interaction_log.action IS 'page | filter | sort | adult | expired | limit_hit | upsell'
    """,
    """
    COMMENT ON COLUMN public.interaction_log.content_type IS 'Active result view: all/channels/groups/posts/photos/videos/audio/documents'
    """,
    """
    COMMENT ON COLUMN public.interaction_log.sort IS 'r | v | n'
    """,
    """
    COMMENT ON COLUMN public.interaction_log.page IS 'Page number AFTER the action (paging depth)'
    """,
    """
    COMMENT ON COLUMN public.interaction_log.adult_only IS 'Content-filter (18+) mode at the time of the action'
    """,
    """
    COMMENT ON COLUMN public.interaction_log.is_last_page IS 'Results ended (has_more=false) — the user reached the end'
    """,
    """
    CREATE SEQUENCE public.interaction_log_id_seq
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.interaction_log_id_seq OWNED BY public.interaction_log.id
    """,
    """
    CREATE TABLE public.media (
        id integer NOT NULL,
        post_id integer NOT NULL,
        type public.media_type NOT NULL,
        file_id character varying(512) NOT NULL,
        file_name character varying(255),
        file_size bigint,
        duration integer,
        caption text
    )
    """,
    """
    COMMENT ON COLUMN public.media.type IS 'Media type'
    """,
    """
    COMMENT ON COLUMN public.media.file_id IS 'Telegram file_id'
    """,
    """
    COMMENT ON COLUMN public.media.file_name IS 'File name'
    """,
    """
    COMMENT ON COLUMN public.media.file_size IS 'File size in bytes'
    """,
    """
    COMMENT ON COLUMN public.media.duration IS 'Duration in seconds'
    """,
    """
    COMMENT ON COLUMN public.media.caption IS 'Media caption'
    """,
    """
    CREATE SEQUENCE public.media_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.media_id_seq OWNED BY public.media.id
    """,
    """
    CREATE TABLE public.posts (
        id integer NOT NULL,
        channel_id integer NOT NULL,
        telegram_message_id integer NOT NULL,
        text text,
        views_count bigint DEFAULT 0 NOT NULL,
        forwards_count bigint DEFAULT 0 NOT NULL,
        post_date timestamp with time zone,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        search_vector tsvector,
        source character varying(16) DEFAULT 'dispatcher'::character varying NOT NULL
    )
    """,
    """
    COMMENT ON COLUMN public.posts.telegram_message_id IS 'Telegram message ID'
    """,
    """
    COMMENT ON COLUMN public.posts.text IS 'Post text'
    """,
    """
    COMMENT ON COLUMN public.posts.views_count IS 'Number of views'
    """,
    """
    COMMENT ON COLUMN public.posts.forwards_count IS 'Number of forwards'
    """,
    """
    COMMENT ON COLUMN public.posts.post_date IS 'Publication date'
    """,
    """
    COMMENT ON COLUMN public.posts.search_vector IS 'Full-text index (tsvector)'
    """,
    """
    COMMENT ON COLUMN public.posts.source IS 'Post origin: dispatcher (data pipeline) | tgstat (external import)'
    """,
    """
    CREATE SEQUENCE public.posts_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.posts_id_seq OWNED BY public.posts.id
    """,
    """
    CREATE TABLE public.search_log (
        id integer NOT NULL,
        telegram_user_id bigint,
        query character varying(100) NOT NULL,
        results_count integer DEFAULT 0 NOT NULL,
        has_results boolean NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        adult_only boolean
    )
    """,
    """
    COMMENT ON COLUMN public.search_log.telegram_user_id IS 'Telegram id of the searching user'
    """,
    """
    COMMENT ON COLUMN public.search_log.query IS 'Normalised query text (strip+lower)'
    """,
    """
    COMMENT ON COLUMN public.search_log.results_count IS 'Number of results found (channels+posts)'
    """,
    """
    COMMENT ON COLUMN public.search_log.has_results IS 'Whether the result was non-empty (false = content gap)'
    """,
    """
    COMMENT ON COLUMN public.search_log.created_at IS 'When the search happened'
    """,
    """
    COMMENT ON COLUMN public.search_log.adult_only IS 'Whether 18+ mode was on during the search; NULL = unknown (legacy rows)'
    """,
    """
    CREATE SEQUENCE public.search_log_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.search_log_id_seq OWNED BY public.search_log.id
    """,
    """
    CREATE TABLE public.search_session (
        id bigint NOT NULL,
        telegram_user_id bigint NOT NULL,
        chat_id bigint NOT NULL,
        message_id bigint NOT NULL,
        created_at timestamp with time zone DEFAULT now() NOT NULL
    )
    """,
    """
    COMMENT ON COLUMN public.search_session.telegram_user_id IS 'Session owner (Telegram id)'
    """,
    """
    COMMENT ON COLUMN public.search_session.chat_id IS 'Chat of the results message'
    """,
    """
    COMMENT ON COLUMN public.search_session.message_id IS 'Id of the results message (session identifier)'
    """,
    """
    COMMENT ON COLUMN public.search_session.created_at IS 'When the results were sent'
    """,
    """
    CREATE SEQUENCE public.search_session_id_seq
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.search_session_id_seq OWNED BY public.search_session.id
    """,
    """
    CREATE TABLE public.user_stats (
        id integer NOT NULL,
        telegram_user_id bigint NOT NULL,
        username character varying(64),
        requests_count integer DEFAULT 0 NOT NULL,
        first_seen timestamp with time zone DEFAULT now() NOT NULL,
        last_seen timestamp with time zone DEFAULT now() NOT NULL,
        language character varying(8)
    )
    """,
    """
    COMMENT ON COLUMN public.user_stats.telegram_user_id IS 'User Telegram id (dedup key)'
    """,
    """
    COMMENT ON COLUMN public.user_stats.username IS '@username at the last request (informational)'
    """,
    """
    COMMENT ON COLUMN public.user_stats.requests_count IS 'How many times the user contacted the bot'
    """,
    """
    COMMENT ON COLUMN public.user_stats.first_seen IS 'First contact'
    """,
    """
    COMMENT ON COLUMN public.user_stats.last_seen IS 'Last contact'
    """,
    """
    COMMENT ON COLUMN public.user_stats.language IS 'Interface language (en/ru/…); NULL = undetermined'
    """,
    """
    CREATE SEQUENCE public.user_stats_id_seq
        AS integer
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.user_stats_id_seq OWNED BY public.user_stats.id
    """,
    """
    CREATE TABLE public.user_subscription (
        id bigint NOT NULL,
        telegram_user_id bigint NOT NULL,
        tier character varying(16) DEFAULT 'premium'::character varying NOT NULL,
        status character varying(16) DEFAULT 'active'::character varying NOT NULL,
        source character varying(16) DEFAULT 'manual'::character varying NOT NULL,
        starts_at timestamp with time zone DEFAULT now() NOT NULL,
        expires_at timestamp with time zone,
        is_recurring boolean DEFAULT false NOT NULL,
        external_id character varying(64),
        created_at timestamp with time zone DEFAULT now() NOT NULL,
        updated_at timestamp with time zone DEFAULT now() NOT NULL
    )
    """,
    """
    COMMENT ON COLUMN public.user_subscription.tier IS 'Access tier (currently premium only)'
    """,
    """
    COMMENT ON COLUMN public.user_subscription.status IS 'active | cancelled | expired'
    """,
    """
    COMMENT ON COLUMN public.user_subscription.source IS 'manual (panel) | stars (Telegram Stars) | …'
    """,
    """
    COMMENT ON COLUMN public.user_subscription.expires_at IS 'NULL = no expiry'
    """,
    """
    COMMENT ON COLUMN public.user_subscription.is_recurring IS 'Auto-renewal (for a future Stars subscription)'
    """,
    """
    COMMENT ON COLUMN public.user_subscription.external_id IS 'Payment provider charge/subscription id (Stars charge id) — for future payments'
    """,
    """
    CREATE SEQUENCE public.user_subscription_id_seq
        START WITH 1
        INCREMENT BY 1
        NO MINVALUE
        NO MAXVALUE
        CACHE 1
    """,
    """
    ALTER SEQUENCE public.user_subscription_id_seq OWNED BY public.user_subscription.id
    """,
]

# ─── Column defaults (sequences) ─────────────────────────────────────────────
_DEFAULTS: list[str] = [
    """
    ALTER TABLE ONLY public.admin_action_log ALTER COLUMN id SET DEFAULT nextval('public.admin_action_log_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.channel_access ALTER COLUMN id SET DEFAULT nextval('public.channel_access_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.channel_edit_log ALTER COLUMN id SET DEFAULT nextval('public.channel_edit_log_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.channel_queue ALTER COLUMN id SET DEFAULT nextval('public.channel_queue_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.channels ALTER COLUMN id SET DEFAULT nextval('public.channels_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.feedback ALTER COLUMN id SET DEFAULT nextval('public.feedback_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.hot_searches ALTER COLUMN id SET DEFAULT nextval('public.hot_searches_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.interaction_log ALTER COLUMN id SET DEFAULT nextval('public.interaction_log_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.media ALTER COLUMN id SET DEFAULT nextval('public.media_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.posts ALTER COLUMN id SET DEFAULT nextval('public.posts_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.search_log ALTER COLUMN id SET DEFAULT nextval('public.search_log_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.search_session ALTER COLUMN id SET DEFAULT nextval('public.search_session_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.user_stats ALTER COLUMN id SET DEFAULT nextval('public.user_stats_id_seq'::regclass)
    """,
    """
    ALTER TABLE ONLY public.user_subscription ALTER COLUMN id SET DEFAULT nextval('public.user_subscription_id_seq'::regclass)
    """,
]

# ─── Primary keys and unique constraints ─────────────────────────────────────
_CONSTRAINTS: list[str] = [
    """
    ALTER TABLE ONLY public.admin_action_log
        ADD CONSTRAINT admin_action_log_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.app_settings
        ADD CONSTRAINT app_settings_pkey PRIMARY KEY (key)
    """,
    """
    ALTER TABLE ONLY public.channel_access
        ADD CONSTRAINT channel_access_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.channel_edit_log
        ADD CONSTRAINT channel_edit_log_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.channel_queue
        ADD CONSTRAINT channel_queue_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.channels
        ADD CONSTRAINT channels_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.channels
        ADD CONSTRAINT channels_telegram_id_key UNIQUE (telegram_id)
    """,
    """
    ALTER TABLE ONLY public.feedback
        ADD CONSTRAINT feedback_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.hot_searches
        ADD CONSTRAINT hot_searches_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.hot_searches
        ADD CONSTRAINT hot_searches_query_key UNIQUE (query)
    """,
    """
    ALTER TABLE ONLY public.interaction_log
        ADD CONSTRAINT interaction_log_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.media
        ADD CONSTRAINT media_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.posts
        ADD CONSTRAINT posts_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.search_log
        ADD CONSTRAINT search_log_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.search_session
        ADD CONSTRAINT search_session_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.channel_access
        ADD CONSTRAINT uq_channel_access_account_channel UNIQUE (account_id, telegram_id)
    """,
    """
    ALTER TABLE ONLY public.channel_queue
        ADD CONSTRAINT uq_channel_queue_username UNIQUE (username)
    """,
    """
    ALTER TABLE ONLY public.posts
        ADD CONSTRAINT uq_posts_channel_msg UNIQUE (channel_id, telegram_message_id)
    """,
    """
    ALTER TABLE ONLY public.search_session
        ADD CONSTRAINT uq_search_session_message UNIQUE (chat_id, message_id)
    """,
    """
    ALTER TABLE ONLY public.user_stats
        ADD CONSTRAINT uq_user_stats_telegram_user_id UNIQUE (telegram_user_id)
    """,
    """
    ALTER TABLE ONLY public.user_stats
        ADD CONSTRAINT user_stats_pkey PRIMARY KEY (id)
    """,
    """
    ALTER TABLE ONLY public.user_subscription
        ADD CONSTRAINT user_subscription_pkey PRIMARY KEY (id)
    """,
]

# ─── Indexes (GIN full-text, pg_trgm, btree) ─────────────────────────────────
_INDEXES: list[str] = [
    """
    CREATE INDEX ix_admin_action_log_created ON public.admin_action_log USING btree (created_at)
    """,
    """
    CREATE INDEX ix_channel_access_lookup ON public.channel_access USING btree (account_id, telegram_id)
    """,
    """
    CREATE INDEX ix_channel_edit_log_channel_id ON public.channel_edit_log USING btree (channel_id)
    """,
    """
    CREATE INDEX ix_channel_edit_log_created_at ON public.channel_edit_log USING btree (created_at)
    """,
    """
    CREATE INDEX ix_channel_queue_assigned_account ON public.channel_queue USING btree (assigned_account)
    """,
    """
    CREATE INDEX ix_channel_queue_category ON public.channel_queue USING btree (category)
    """,
    """
    CREATE INDEX ix_channel_queue_last_parsed_at ON public.channel_queue USING btree (last_parsed_at)
    """,
    """
    CREATE INDEX ix_channel_queue_parse_status ON public.channel_queue USING btree (parse_status)
    """,
    """
    CREATE INDEX ix_channel_queue_priority ON public.channel_queue USING btree (priority)
    """,
    """
    CREATE INDEX ix_channels_category ON public.channels USING btree (category)
    """,
    """
    CREATE INDEX ix_channels_keywords_vector ON public.channels USING gin (keywords_vector)
    """,
    """
    CREATE INDEX ix_channels_members_count ON public.channels USING btree (members_count)
    """,
    """
    CREATE INDEX ix_channels_search_vector ON public.channels USING gin (search_vector)
    """,
    """
    CREATE INDEX ix_channels_source ON public.channels USING btree (source)
    """,
    """
    CREATE INDEX ix_channels_title ON public.channels USING btree (title)
    """,
    """
    CREATE INDEX ix_channels_title_trgm ON public.channels USING gin (title public.gin_trgm_ops)
    """,
    """
    CREATE INDEX ix_channels_username_trgm ON public.channels USING gin (username public.gin_trgm_ops)
    """,
    """
    CREATE INDEX ix_feedback_created_at ON public.feedback USING btree (created_at)
    """,
    """
    CREATE INDEX ix_feedback_user_created ON public.feedback USING btree (telegram_user_id, created_at)
    """,
    """
    CREATE INDEX ix_hot_searches_count ON public.hot_searches USING btree (count)
    """,
    """
    CREATE INDEX ix_hot_searches_query ON public.hot_searches USING btree (query)
    """,
    """
    CREATE INDEX ix_interaction_log_action_created ON public.interaction_log USING btree (action, created_at)
    """,
    """
    CREATE INDEX ix_interaction_log_created ON public.interaction_log USING btree (created_at)
    """,
    """
    CREATE INDEX ix_media_post_id ON public.media USING btree (post_id)
    """,
    """
    CREATE INDEX ix_media_type ON public.media USING btree (type)
    """,
    """
    CREATE INDEX ix_posts_channel_id ON public.posts USING btree (channel_id)
    """,
    """
    CREATE INDEX ix_posts_post_date ON public.posts USING btree (post_date)
    """,
    """
    CREATE INDEX ix_posts_search_vector ON public.posts USING gin (search_vector)
    """,
    """
    CREATE INDEX ix_posts_source ON public.posts USING btree (source)
    """,
    """
    CREATE INDEX ix_posts_views_count ON public.posts USING btree (views_count)
    """,
    """
    CREATE INDEX ix_search_log_created_at ON public.search_log USING btree (created_at)
    """,
    """
    CREATE INDEX ix_search_log_has_results ON public.search_log USING btree (has_results)
    """,
    """
    CREATE INDEX ix_search_log_query ON public.search_log USING btree (query)
    """,
    """
    CREATE INDEX ix_search_session_user_created ON public.search_session USING btree (telegram_user_id, created_at)
    """,
    """
    CREATE INDEX ix_user_stats_last_seen ON public.user_stats USING btree (last_seen)
    """,
    """
    CREATE INDEX ix_user_subscription_user_status ON public.user_subscription USING btree (telegram_user_id, status, expires_at)
    """,
]

# ─── Triggers ────────────────────────────────────────────────────────────────
_TRIGGERS: list[str] = [
    """
    CREATE TRIGGER trg_channel_search_vector BEFORE INSERT OR UPDATE ON public.channels FOR EACH ROW EXECUTE FUNCTION public.update_channel_search_vector()
    """,
    """
    CREATE TRIGGER trg_post_search_vector BEFORE INSERT OR UPDATE ON public.posts FOR EACH ROW EXECUTE FUNCTION public.update_post_search_vector()
    """,
]

# ─── Foreign keys ────────────────────────────────────────────────────────────
_FKS: list[str] = [
    """
    ALTER TABLE ONLY public.channel_queue
        ADD CONSTRAINT channel_queue_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.channels(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE ONLY public.media
        ADD CONSTRAINT media_post_id_fkey FOREIGN KEY (post_id) REFERENCES public.posts(id) ON DELETE CASCADE
    """,
    """
    ALTER TABLE ONLY public.posts
        ADD CONSTRAINT posts_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.channels(id) ON DELETE CASCADE
    """,
]

# ─── Grants for the panel roles ──────────────────────────────────────────────
# Applied only if the role exists (roles are created outside migrations, with
# passwords from the environment — see scripts/setup_roles.py). The read-only role
# additionally receives SELECT on all tables from that script.
# Kept in the original order: PostgreSQL stores grants in an ACL array, and
# the order of grants per table is part of the resulting schema.
_ROLE_GRANTS: list[tuple[str, str]] = [
    ("teleseek_ro", "GRANT SELECT ON TABLE public.admin_action_log TO teleseek_ro"),
    ("teleseek_editor", "GRANT SELECT,INSERT ON TABLE public.admin_action_log TO teleseek_editor"),
    ("teleseek_editor", "GRANT SELECT,USAGE ON SEQUENCE public.admin_action_log_id_seq TO teleseek_editor"),
    ("teleseek_ro", "GRANT SELECT ON TABLE public.app_settings TO teleseek_ro"),
    ("teleseek_editor", "GRANT SELECT ON TABLE public.app_settings TO teleseek_editor"),
    ("teleseek_editor", "GRANT UPDATE(value) ON TABLE public.app_settings TO teleseek_editor"),
    ("teleseek_editor", "GRANT UPDATE(updated_at) ON TABLE public.app_settings TO teleseek_editor"),
    ("teleseek_editor", "GRANT UPDATE(updated_by) ON TABLE public.app_settings TO teleseek_editor"),
    ("teleseek_editor", "GRANT SELECT ON TABLE public.channel_queue TO teleseek_editor"),
    ("teleseek_ro", "GRANT SELECT ON TABLE public.feedback TO teleseek_ro"),
    ("teleseek_editor", "GRANT SELECT,DELETE ON TABLE public.feedback TO teleseek_editor"),
    ("teleseek_editor", "GRANT UPDATE(is_favorite) ON TABLE public.feedback TO teleseek_editor"),
    ("teleseek_ro", "GRANT SELECT ON TABLE public.interaction_log TO teleseek_ro"),
    ("teleseek_editor", "GRANT SELECT ON TABLE public.interaction_log TO teleseek_editor"),
    ("teleseek_ro", "GRANT SELECT ON TABLE public.search_session TO teleseek_ro"),
    ("teleseek_ro", "GRANT SELECT ON TABLE public.user_subscription TO teleseek_ro"),
    ("teleseek_editor", "GRANT SELECT,INSERT ON TABLE public.user_subscription TO teleseek_editor"),
    ("teleseek_editor", "GRANT UPDATE(status) ON TABLE public.user_subscription TO teleseek_editor"),
    ("teleseek_editor", "GRANT UPDATE(expires_at) ON TABLE public.user_subscription TO teleseek_editor"),
    ("teleseek_editor", "GRANT UPDATE(is_recurring) ON TABLE public.user_subscription TO teleseek_editor"),
    ("teleseek_editor", "GRANT UPDATE(updated_at) ON TABLE public.user_subscription TO teleseek_editor"),
    ("teleseek_editor", "GRANT SELECT,USAGE ON SEQUENCE public.user_subscription_id_seq TO teleseek_editor"),
]


def _grant_if_role_exists(role: str, statement: str) -> str:
    return (
        "DO $$\nBEGIN\n"
        f"    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"        {statement};\n"
        "    END IF;\nEND\n$$"
    )


def upgrade() -> None:
    for group in (
        _EXTENSIONS, _TYPES, _FUNCTIONS, _TABLES, _DEFAULTS,
        _CONSTRAINTS, _INDEXES, _TRIGGERS, _FKS,
    ):
        for statement in group:
            # Statements are indented for readability; dedent restores the exact
            # text (function bodies are stored verbatim by PostgreSQL).
            op.execute(textwrap.dedent(statement))
    # Default limit settings. limits_enabled=false: the limits infrastructure is
    # in place, but the product runs without limits until an operator enables them.
    op.execute(
        """
        INSERT INTO app_settings (key, value, updated_by) VALUES
            ('limits_enabled',      'false', 'baseline_migration'),
            ('free_daily_searches', '10',    'baseline_migration'),
            ('free_max_pages',      '3',     'baseline_migration')
        ON CONFLICT (key) DO NOTHING
        """
    )
    for role, statement in _ROLE_GRANTS:
        op.execute(_grant_if_role_exists(role, statement))


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS admin_action_log, app_settings, channel_access, "
        "channel_edit_log, channel_queue, channels, feedback, hot_searches, "
        "interaction_log, media, posts, search_log, search_session, "
        "user_stats, user_subscription CASCADE"
    )
    op.execute("DROP FUNCTION IF EXISTS update_channel_search_vector() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS update_post_search_vector() CASCADE")
    op.execute("DROP TYPE IF EXISTS channel_type, media_type, parse_status CASCADE")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
