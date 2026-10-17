"""
TeleSeek search latency benchmark — BEFORE/AFTER on the same set of words.

Isolated: DATABASE_URL is read straight from the environment; the bot settings
(`bot.config`/pydantic) are NOT imported, so a misconfigured .env can never leak
values into a traceback.
It goes through the REAL search code (`bot.database.queries`), so it measures the
same thing the bot does: MAX_RESULTS, deduplication, content filter, regional
filter, prefix fallback, ILIKE fallback.

Three groups:
  (a) zero-result queries — words that the index does not contain, so every
      fallback path is exercised (the slowest case before the fallback got a
      time budget);
  (b) queries with results — channels/groups/posts in five languages;
  (c) content-filter (18+) toggle on/off for the same words.

Metrics: time, number of results, blks_read (pg_stat_database delta), path
(tsvector / ILIKE fallback / empty). Plus the top-N result ids — to compare the
BEFORE/AFTER result sets for regressions (scripts/bench_report.py).

Run (one-off container on the bot image, env from compose):
    docker compose run --rm -T bot python -m scripts.bench_search > bench_before.json
"""

import asyncio
import json
import os
import sys
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database import queries

# Zero-result queries: neutral words that are absent from the index (en/ru/uz/kk/ar).
EMPTY_QUERIES = [
    "quantum gardening",
    "vintage typewriters",
    "кружевные зонты",
    "qadimiy soatlar",
    "ежелгі сағаттар",
    "ساعات قديمة",
]

# Queries with results — different content types and languages (en/ru/uz/kk/ar).
RESULT_QUERIES = [
    "news",
    "recipe",
    "новости",
    "рецепты",
    "yangiliklar",
    "жаңалықтар",
    "أخبار",
    "وصفات",
]

# Content-filter toggle: the same words in both modes.
ADULT_TOGGLE_QUERIES = ["horror", "news"]

PER_PAGE = 11  # like the bot: per_page + 1 (N+1 for has_more)


async def _blks_read(session) -> int:
    row = await session.execute(
        text("SELECT blks_read FROM pg_stat_database WHERE datname = current_database()")
    )
    return int(row.scalar_one() or 0)


async def _ts_only_posts(session, query: str, adult_only: bool) -> int:
    """How many posts the PURE tsvector path returns (no fallbacks) — path classification."""
    stmt = text("""
        SELECT count(*) FROM (
            SELECT p.id FROM posts p JOIN channels c ON c.id = p.channel_id
            WHERE p.search_vector @@ plainto_tsquery('russian', :q)
              AND (CASE WHEN :adult THEN c.is_adult IS TRUE ELSE c.is_adult IS NOT TRUE END)
              AND (c.section IS NULL OR NOT (c.section = ANY(:hidden)))
            LIMIT 50
        ) t
    """)
    row = await session.execute(
        stmt, {"q": query, "adult": adult_only, "hidden": list(queries.HIDDEN_SECTIONS)}
    )
    return int(row.scalar_one() or 0)


async def _measure(session, query: str, adult_only: bool) -> dict:
    """One measurement: channels + posts through the REAL code, with timings and blks_read."""
    out: dict = {"query": query, "adult_only": adult_only}

    n_ts = await _ts_only_posts(session, query, adult_only)
    out["ts_only_posts"] = n_ts

    b0 = await _blks_read(session)
    t0 = time.perf_counter()
    channels = await queries.search_channels_by_text(
        session, query, limit=PER_PAGE, offset=0, adult_only=adult_only, sort="r"
    )
    t_ch = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    posts = await queries.search_posts(
        session, query, limit=PER_PAGE, offset=0, sort="r", adult_only=adult_only
    )
    t_posts = (time.perf_counter() - t0) * 1000
    b1 = await _blks_read(session)

    out["ms_channels"] = round(t_ch, 1)
    out["ms_posts"] = round(t_posts, 1)
    out["ms_total"] = round(t_ch + t_posts, 1)
    out["n_channels"] = len(channels)
    out["n_posts"] = len(posts)
    out["blks_read_delta"] = b1 - b0
    # Post path: tsvector found something / a fallback filled in / everything empty.
    if n_ts > 0:
        out["post_path"] = "tsvector"
    elif len(posts) > 0:
        out["post_path"] = "fallback_hit"
    else:
        out["post_path"] = "fallback_empty"
    # Top-N ids — for regression comparison (membership AND order).
    out["top_channel_ids"] = [c.id for c in channels[:10]]
    out["top_post_ids"] = [p.id for p in posts[:10]]
    return out


async def main() -> int:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    engine = create_async_engine(db_url, echo=False, pool_size=2, max_overflow=2)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    report: dict = {
        "max_results": queries.MAX_RESULTS,
        "post_fallback_enabled": getattr(queries, "POST_ILIKE_FALLBACK", None),
        "post_fallback_timeout_ms": getattr(queries, "POST_FALLBACK_TIMEOUT_MS", None),
        "groups": {},
    }

    async with factory() as session:
        for group, qs, adult in (
            ("empty", EMPTY_QUERIES, False),
            ("result", RESULT_QUERIES, False),
        ):
            rows = []
            for q in qs:
                rows.append(await _measure(session, q, adult))
                print(f"[{group}] {q!r} done", file=sys.stderr, flush=True)
            report["groups"][group] = rows

        toggle = []
        for q in ADULT_TOGGLE_QUERIES:
            for adult in (False, True):
                toggle.append(await _measure(session, q, adult))
                print(f"[adult={adult}] {q!r} done", file=sys.stderr, flush=True)
        report["groups"]["adult_toggle"] = toggle

    await engine.dispose()
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
