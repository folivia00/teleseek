"""
Command-line search — try TeleSeek without a Telegram bot token.

Runs exactly the same code path as the bot: the query blocklist
(bot.safety), then bot.services.search_engine.SearchEngine on top of
bot.database.queries (weighted full-text search, prefix fallback, ILIKE and
pg_trgm fallbacks, hidden exact duplicates, content filter, sorting,
pagination). Only the output is different: plain text instead of a Telegram
message.

Usage (inside the compose stack):
    docker compose run --rm bot python -m scripts.search_cli "recipe"
    docker compose run --rm bot python -m scripts.search_cli "news" --page 2
    docker compose run --rm bot python -m scripts.search_cli "travel" --type photos --sort v
    docker compose run --rm bot python -m scripts.search_cli "horror" --adult

DATABASE_URL is read from the environment; the bot settings (and therefore
BOT_TOKEN) are not needed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Channel, Post
from bot.safety import is_blocked_query
from bot.services.search_engine import ContentType, SearchEngine, SearchResult

SORTS = {"r": "relevance", "v": "views", "n": "newest"}


def _channel_line(ch: Channel) -> str:
    kind = "group" if ch.type.value == "group" else "channel"
    handle = f"@{ch.username}" if ch.username else "(no username)"
    flag = " [18+]" if ch.is_adult else ""
    return (f"  [{kind}] {ch.title}{flag}  {handle}  "
            f"members={ch.members_count:,}  category={ch.category or '-'}")


def _post_line(post: Post) -> str:
    snippet = " ".join((post.text or "").split())
    if len(snippet) > 110:
        snippet = snippet[:107] + "..."
    date = post.post_date.strftime("%Y-%m-%d") if post.post_date else "-"
    channel = post.channel.title if post.channel else f"channel #{post.channel_id}"
    return f"  {date}  views={post.views_count:,}  {channel}: {snippet}"


def _print_result(res: SearchResult, elapsed_ms: float) -> None:
    if not res.has_results:
        print("Nothing found.")
    if res.channels:
        print(f"Channels and groups ({len(res.channels)}):")
        for ch in res.channels:
            print(_channel_line(ch))
    if res.posts:
        print(f"Posts ({len(res.posts)}):")
        for post in res.posts:
            print(_post_line(post))
    more = "yes" if res.has_more else "no"
    print(f"-- page {res.page}, next page: {more}"
          + (", result cap reached — refine the query" if res.capped else "")
          + f", {elapsed_ms:.0f} ms")


async def run(args: argparse.Namespace) -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    if is_blocked_query(args.query):
        # Same behaviour as the bot: the search is not executed at all.
        print("This query cannot be processed.")
        return 0

    engine = create_async_engine(url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            search = SearchEngine(session)
            started = time.perf_counter()
            result = await search.search_by_type(
                args.query, ContentType(args.type), page=args.page,
                per_page=args.per_page, sort=args.sort, adult_only=args.adult,
            )
            elapsed = (time.perf_counter() - started) * 1000
            mode = "18+ only" if args.adult else "safe search"
            print(f'Query: "{args.query}"  type={args.type}  sort={SORTS[args.sort]}  mode={mode}')
            _print_result(result, elapsed)
    finally:
        await engine.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Search the TeleSeek index from the command line.")
    parser.add_argument("query", help="search query (any language)")
    parser.add_argument("--type", default="all", choices=[c.value for c in ContentType],
                        help="content type filter (default: all)")
    parser.add_argument("--sort", default="r", choices=list(SORTS),
                        help="r = relevance, v = views, n = newest (default: r)")
    parser.add_argument("--page", type=int, default=1, help="page number, starting at 1")
    parser.add_argument("--per-page", type=int, default=10, help="results per page (default: 10)")
    parser.add_argument("--adult", action="store_true",
                        help="18+ mode: show only channels flagged 18+ (default is safe search)")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
