"""
Build topical channel keywords from their posts (search relevance).

Problem: a "gardening" channel is found by "garden" among POSTS but not among CHANNELS
(channel search matches only title/description). Solution: for each channel collect frequent
TOPICAL words from its posts → channels.keywords → channel search matches them too.

Algorithm (TF-IDF-like, morphology — Postgres 'russian' Snowball):
  * TF: per-channel `ts_stat` over posts.search_vector → stemmed lexemes + frequency
    (nentry). Postgres stems words itself (inflected forms → one stem) and removes
    Russian stop words — text is NOT pulled into Python (light on postgres, index-driven).
  * Filters: length ≥3, not a number, not in MANUAL_STOP (stop words that survived the dictionary).
  * IDF (specificity): lexemes present in TOO MANY channels (> DF_MAX share) are treated
    as generic/non-topical → dropped. What remains are words frequent FOR THE CHANNEL and
    rare GLOBALLY (topical).
  * Top-N (default 20) per channel → written to channels.keywords (space-separated).

IDEMPOTENT: re-running overwrites keywords (from current posts). keywords_vector
(tsvector) is rebuilt by the database automatically (GENERATED column).

Light on postgres: one ts_stat per channel (via index ix_posts_channel_id), UPDATEs in batches.
Does NOT touch posts. Run (one-off container):
  DATABASE_URL=... python -m scripts.build_channel_keywords
  docker compose run --rm bot python -m scripts.build_channel_keywords
"""
from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("kw")

DB_URL = os.environ["DATABASE_URL"]
TOP_N = int(os.getenv("KW_TOP_N", "20"))            # keywords per channel
CAND_K = int(os.getenv("KW_CAND_K", "40"))          # candidates from ts_stat per channel
DF_MAX = float(os.getenv("KW_DF_MAX", "0.20"))      # lexeme in >20% of channels = generic, dropped
MIN_NENTRY = int(os.getenv("KW_MIN_NENTRY", "2"))   # lexeme must occur ≥2 times
BATCH = int(os.getenv("KW_BATCH", "200"))           # UPDATE batch size

# Stop-word list (algorithm data, not UI text): stemmed lexemes that survive the
# PostgreSQL 'russian' dictionary but carry no topic — function/generic words and
# channel meta words. They are compared against ts_stat lexemes, so they are kept
# in their stemmed Russian form on purpose and must not be translated.
MANUAL_STOP = {
    "эт", "котор", "как", "так", "ещ", "оч", "сам", "друг", "свой", "весь", "наш", "ваш",
    "мой", "тот", "чтоб", "когд", "очен", "прост", "нужн", "мног", "хорош", "больш", "нов",
    "год", "дел", "мест", "врем", "today", "the", "and", "you", "for", "all", "this", "that",
    "http", "https", "www", "com", "ru",
    # channel meta words (not about the content topic):
    "канал", "чат", "бот", "подписа", "подпис", "ссылк", "вебинар", "эфир", "вопрос",
    "telegram", "телеграм", "репост", "коммент", "лайк", "приобрест", "купит", "заказ",
    "скидк", "акци", "промокод", "реклам",
}


def _ok(word: str) -> bool:
    w = (word or "").strip()
    if len(w) < 3:
        return False
    if w.isdigit() or not any(ch.isalpha() for ch in w):
        return False
    # URLs/domains/mentions — Postgres sometimes returns them as a single token (www.x.ru, @user).
    if any(c in w for c in "./@:%_"):
        return False
    if w.lower() in MANUAL_STOP:
        return False
    return True


async def main() -> int:
    engine = create_async_engine(DB_URL, pool_size=2, max_overflow=1, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            # Channels that have posts (otherwise there is nothing to take keywords from).
            cids = [r[0] for r in (await conn.execute(text(
                "SELECT c.id FROM channels c WHERE EXISTS "
                "(SELECT 1 FROM posts p WHERE p.channel_id = c.id) ORDER BY c.id"))).all()]
        logger.info("Channels with posts: %d | TOP_N=%d CAND_K=%d DF_MAX=%.2f", len(cids), TOP_N, CAND_K, DF_MAX)

        # ── PASS 1: per-channel candidates (ts_stat) + global document frequency of lexemes ──
        cand: dict[int, list[tuple[str, int]]] = {}
        global_df: dict[str, int] = {}
        async with engine.connect() as conn:
            for i, cid in enumerate(cids, 1):
                rows = (await conn.execute(text(
                    f"SELECT word, nentry FROM ts_stat("
                    f"'SELECT search_vector FROM posts WHERE channel_id={int(cid)}') "
                    f"ORDER BY nentry DESC, ndoc DESC LIMIT :k"), {"k": CAND_K})).all()
                lst = [(w, int(n)) for w, n in rows if _ok(w) and int(n) >= MIN_NENTRY]
                cand[cid] = lst
                for w, _n in lst:
                    global_df[w] = global_df.get(w, 0) + 1
                if i % 500 == 0:
                    logger.info("  pass1 %d/%d", i, len(cids))

        total = max(1, len(cids))
        common = {w for w, df in global_df.items() if df / total > DF_MAX}
        logger.info("Globally generic lexemes (>%.0f%% of channels) dropped: %d", DF_MAX * 100, len(common))

        # ── PASS 2: top-N topical (without generic) → UPDATE in batches ──
        updated = 0
        async with engine.begin() as conn:
            buf: list[dict] = []
            for cid in cids:
                kws = [w for w, _n in cand[cid] if w not in common][:TOP_N]
                buf.append({"id": cid, "kw": " ".join(kws) if kws else None})
                if len(buf) >= BATCH:
                    await conn.execute(text("UPDATE channels SET keywords = :kw WHERE id = :id"), buf)
                    updated += len(buf)
                    buf = []
            if buf:
                await conn.execute(text("UPDATE channels SET keywords = :kw WHERE id = :id"), buf)
                updated += len(buf)
        logger.info("Done: keywords written for %d channels.", updated)
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
