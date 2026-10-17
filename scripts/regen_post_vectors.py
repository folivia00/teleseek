"""
Regenerate posts.search_vector for the weighted schema.

After a change of the weighting scheme (new definition of the update_post_search_vector trigger: lead=A, body=B)
EXISTING posts still hold the old vector. This script "touches" every post in batches by id
range (`UPDATE posts SET views_count = views_count`) → the BEFORE UPDATE trigger rebuilds
search_vector with weights. NOTHING in the data changes (views_count stays the same), only the vector.

Light on postgres (hundreds of thousands of posts): small batches, a pause between them, a commit
per batch. Idempotent (safe to repeat). Can run alongside the data pipeline, which writes new posts
concurrently (already weighted via the trigger; only short locks on id ranges).

Run (one-off container):
  DATABASE_URL=... python -m scripts.regen_post_vectors
  docker compose run --rm bot python -m scripts.regen_post_vectors
Afterwards VACUUM ANALYZE posts is recommended (separately).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("regen")

DB_URL = os.environ["DATABASE_URL"]
BATCH = int(os.getenv("REGEN_BATCH", "5000"))     # id range per batch
SLEEP = float(os.getenv("REGEN_SLEEP", "0.3"))    # pause between batches (light on postgres)


async def main() -> int:
    engine = create_async_engine(DB_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            lo, hi, cnt = (await conn.execute(
                text("SELECT min(id), max(id), count(*) FROM posts"))).first()
        if lo is None:
            logger.info("No posts — nothing to regenerate.")
            return 0
        logger.info("Posts: %d | id %d..%d | BATCH=%d", cnt, lo, hi, BATCH)

        done = 0
        cur = lo
        t0 = time.time()
        while cur <= hi:
            async with engine.begin() as conn:
                res = await conn.execute(
                    text("UPDATE posts SET views_count = views_count "
                         "WHERE id >= :a AND id < :b"),
                    {"a": cur, "b": cur + BATCH})
                done += res.rowcount or 0
            cur += BATCH
            if (cur - lo) % (BATCH * 20) == 0:
                logger.info("  regenerated ~%d posts (id<%d), %.0fs", done, cur, time.time() - t0)
            time.sleep(SLEEP)
        logger.info("Done: rebuilt search_vector for %d posts in %.0fs. "
                    "VACUUM ANALYZE posts is recommended.", done, time.time() - t0)
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
