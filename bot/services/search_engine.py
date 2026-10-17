"""
TeleSeek search engine.
Combines search over channels, posts and media with result ranking.
"""

import enum
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import Channel, ChannelType, Media, MediaType, Post
from bot.database import queries

logger = logging.getLogger(__name__)


class ContentType(str, enum.Enum):
    """Content types used to filter search."""
    ALL = "all"
    CHANNELS = "channels"       # Channels only
    GROUPS = "groups"           # Groups only
    POSTS = "posts"             # Posts
    PHOTOS = "photos"           # Photos
    VIDEOS = "videos"           # Videos
    AUDIO = "audio"             # Audio
    DOCUMENTS = "documents"     # Documents/files


@dataclass
class SearchResult:
    """Container for search results across all types.

    There is no "N found" counter (the expensive COUNT is not computed). Instead of a
    total there is `has_more` (whether a next page exists, determined via N+1 without
    COUNT) and `capped` (MAX_RESULTS reached: show a "refine your query" hint)."""
    query: str
    channels: List[Channel] = field(default_factory=list)
    posts: List[Post] = field(default_factory=list)
    media: List[Media] = field(default_factory=list)
    page: int = 1
    per_page: int = 10
    has_more: bool = False     # whether a next page exists (no COUNT, via N+1)
    capped: bool = False       # hit the MAX_RESULTS limit (more exist but are not returned)

    @property
    def has_results(self) -> bool:
        """Whether there are any results at all."""
        return bool(self.channels or self.posts or self.media)


class SearchEngine:
    """
    TeleSeek search engine.

    Searches the database using PostgreSQL tsvector/tsquery.
    Supports filtering by content type and pagination.
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        Initialise the engine.

        Args:
            session: SQLAlchemy async session.
        """
        self._session = session

    async def search_all(
        self,
        query: str,
        page: int = 1,
        per_page: int = 10,
        adult_only: bool = False,
        sort: str = "r",
    ) -> SearchResult:
        """
        Search across all content types (channels + posts).
        The main method, used for a regular query.

        Args:
            query: User search query.
            page: Page number (starting from 1).
            per_page: Number of results per page.
            adult_only: 18+ mode toggle (False = default normal: everything except 18+;
                        True = adult_only: 18+ only).

        Returns:
            SearchResult with channels and posts.
        """
        query = self._normalize_query(query)
        if not query:
            return SearchResult(query=query)

        offset = (page - 1) * per_page

        # NO COUNT. Fetch one more than we show, so we know whether a next page exists
        # (N+1). Channels: top by relevance; posts: only if there are few channels.
        ch_raw = await queries.search_channels_by_text(
            self._session, query, limit=per_page + 1, offset=offset,
            adult_only=adult_only, sort=sort,
        )
        channels = ch_raw[:per_page]
        raw_more = len(ch_raw) > per_page

        posts: List[Post] = []
        if len(channels) < per_page // 2:
            remaining = per_page - len(channels)
            p_raw = await queries.search_posts(
                self._session, query, limit=remaining + 1, offset=offset,
                adult_only=adult_only, sort=sort,
            )
            posts = p_raw[:remaining]
            raw_more = raw_more or (len(p_raw) > remaining)

        at_cap = (offset + per_page) >= queries.MAX_RESULTS
        return SearchResult(
            query=query, channels=channels, posts=posts,
            page=page, per_page=per_page,
            has_more=raw_more and not at_cap,
            capped=at_cap and bool(channels or posts),
        )

    async def search_by_type(
        self,
        query: str,
        content_type: ContentType,
        page: int = 1,
        per_page: int = 10,
        sort: str = "r",
        adult_only: bool = False,
    ) -> SearchResult:
        """
        Search a specific content type (filtering).

        Args:
            query: Search query.
            content_type: Content type to filter by.
            page: Page number.
            per_page: Results per page.
            adult_only: 18+ mode toggle (False = normal: everything except 18+; True = 18+ only).

        Returns:
            SearchResult with results of the selected type.
        """
        query = self._normalize_query(query)
        if not query:
            return SearchResult(query=query)

        if content_type == ContentType.ALL:
            return await self.search_all(
                query, page=page, per_page=per_page, adult_only=adult_only, sort=sort
            )

        offset = (page - 1) * per_page
        result = SearchResult(query=query, page=page, per_page=per_page)

        # NO COUNT. Fetch per_page+1 to know whether a next page exists (N+1).
        if content_type in (ContentType.CHANNELS, ContentType.GROUPS):
            ctype = (ChannelType.channel if content_type == ContentType.CHANNELS
                     else ChannelType.group)
            raw = await queries.search_channels(
                self._session, query, limit=per_page + 1, offset=offset,
                channel_type=ctype, adult_only=adult_only, sort=sort,
            )
            result.channels, result.has_more, result.capped = self._paginate(
                raw, per_page, offset
            )
        else:
            media_map = {
                ContentType.PHOTOS: MediaType.photo,
                ContentType.VIDEOS: MediaType.video,
                ContentType.AUDIO: MediaType.audio,
                ContentType.DOCUMENTS: MediaType.document,
            }
            if content_type == ContentType.POSTS:
                raw = await queries.search_posts(
                    self._session, query, limit=per_page + 1, offset=offset,
                    sort=sort, adult_only=adult_only,
                )
            else:
                raw = await queries.search_posts_with_media(
                    self._session, query, media_type=media_map[content_type],
                    limit=per_page + 1, offset=offset, adult_only=adult_only, sort=sort,
                )
            result.posts, result.has_more, result.capped = self._paginate(
                raw, per_page, offset
            )

        return result

    @staticmethod
    def _paginate(raw: list, per_page: int, offset: int) -> tuple:
        """N+1 -> (shown items, has_more, capped) WITHOUT COUNT.
        has_more: more than per_page rows came back AND the MAX_RESULTS limit is not reached.
        capped: we reached the MAX_RESULTS edge and the page still has results, so the set
        hit the limit -> hint "refine your query" (N+1 sees nothing beyond the limit because
        the selection is truncated at MAX_RESULTS, so capped is decided by the boundary,
        not by N+1)."""
        items = raw[:per_page]
        at_cap = (offset + per_page) >= queries.MAX_RESULTS
        has_more = (len(raw) > per_page) and not at_cap
        capped = at_cap and len(items) > 0
        return items, has_more, capped

    async def record_search(self, query: str) -> None:
        """
        Record the query in the hot-searches counter.
        Call after every user search query.

        Args:
            query: User query.
        """
        normalized = self._normalize_query(query)
        if normalized:
            try:
                await queries.increment_search_count(self._session, normalized)
            except Exception as e:
                # Do not abort the search because of a statistics error
                logger.warning("Failed to record hot query: %s", e)

    @staticmethod
    def _normalize_query(query: str) -> str:
        """
        Normalise a search query.
        Strips surrounding whitespace and truncates overly long input.
        """
        return query.strip()[:100]  # Max 100 characters
