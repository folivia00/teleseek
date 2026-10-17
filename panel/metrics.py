"""
Metrics collection for the TeleSeek monitoring panel — READ-ONLY.

Principles:
  * The panel never changes PROJECT data — SELECT only (read-only role
    teleseek_ro, default_transaction_read_only=on). The only thing the panel
    writes is its OWN snapshot history file (/data, the panel's volume), not
    the project database.
  * No dependency on the bot package — raw SQL in an isolated image.

Sources:
  * Live numbers — the database (counts, ingestion queue, demand).
  * Charts — retrospective, from posts.created_at / search_log / user_stats
    (real history) plus coverage snapshots appended to the history file
    (forward-only).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# ─── Config (env) ────────────────────────────────────────────────────────────
# Path whose filesystem usage is shown as "disk" (inside the panel container).
_DISK_PATH = os.getenv("PANEL_DISK_PATH", "/")

_HIST_PATH = os.getenv("PANEL_HISTORY_PATH", "/data/metrics_history.jsonl")
_HIST_MAX = int(os.getenv("PANEL_HISTORY_MAX", "4032"))   # ~14 days at one snapshot per 5 min

# Alert thresholds (env-tunable).
_AL_RATE_FRAC = float(os.getenv("ALERT_RATE_DROP_FRAC", "0.3"))          # rate < 30% of average
_AL_RATE_MIN_AVG = float(os.getenv("ALERT_RATE_MIN_AVG", "500"))         # do not alert on a low baseline
_AL_ERROR_ABS = int(os.getenv("ALERT_ERROR_ABS", "200"))                 # errors in the queue

# ─── Excluding ADMIN queries from demand analytics ───────────────────────────
# ADMIN_IDS is passed into the panel container from .env (docker-compose).
# Test searches by the operator would pollute demand statistics, so they are
# excluded in EVERY block of that section. Values are integer ids (validated
# with int()), so inlining them into SQL is safe.
_ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
]


def _exclude_admin_sql() -> str:
    """SQL predicate "row is NOT from an admin" for search_log.

    Keeps real users and rows with a NULL user_id (unknown = treated as real)
    and drops queries from ADMIN_IDS. With no admins configured — ``TRUE``
    (nothing is excluded)."""
    if not _ADMIN_IDS:
        return "TRUE"
    ids = ",".join(str(i) for i in _ADMIN_IDS)
    return f"(telegram_user_id IS NULL OR telegram_user_id NOT IN ({ids}))"


async def _scalar(conn, sql: str, **params) -> Any:
    return (await conn.execute(text(sql), params)).scalar()


# ════════════════ Section 1 — Database ══════════════════════════════════════

async def collect_base(conn) -> dict:
    db_size = await _scalar(conn, "SELECT pg_database_size(current_database())") or 0
    db_size_pretty = await _scalar(conn, "SELECT pg_size_pretty(pg_database_size(current_database()))")
    posts = await _scalar(conn, "SELECT count(*) FROM posts") or 0
    channels = await _scalar(conn, "SELECT count(*) FROM channels") or 0
    media = await _scalar(conn, "SELECT count(*) FROM media") or 0

    rows = (await conn.execute(text(
        "SELECT parse_status::text AS st, count(*) AS c FROM channel_queue GROUP BY st"
    ))).all()
    queue = {st: int(c) for st, c in rows}
    queue_total = sum(queue.values())
    done = queue.get("done_depth", 0)
    coverage_pct = round(done / queue_total * 100, 1) if queue_total else 0.0

    return {
        "db_size_bytes": int(db_size), "db_size_pretty": db_size_pretty,
        "posts": int(posts), "channels": int(channels), "media": int(media),
        "queue": {
            "new": queue.get("new", 0), "in_progress": queue.get("in_progress", 0),
            "done_depth": queue.get("done_depth", 0),
            "restricted_skip": queue.get("restricted_skip", 0), "error": queue.get("error", 0),
        },
        "queue_total": queue_total, "coverage_pct": coverage_pct,
    }


async def collect_snapshot_min(conn, full: bool = True) -> dict:
    """ONLY the fields that are actually written to the history file.

    Why a separate collector: the snapshot loop runs around the clock, whether
    or not anyone has the dashboard open, so it must not run the full metric
    set (several seconds of database work) every few minutes.

    Two frequencies (the ``full`` argument):
      * EVERY tick writes only the queue (one GROUP BY over channel_queue,
        tens of milliseconds) — that is what yields `coverage_pct`/`done_depth`,
        the reason the snapshot exists (done_depth has no per-row history and
        cannot be reconstructed retrospectively);
      * RARELY (``full=True``) adds counters that change slowly and cost a full
        scan of posts: `posts` and `posts_1h` (no index on created_at), plus two
        cheap user counters.
    The series stays continuous; slow series simply have sparser points.
    Values are EXACT (not estimates): the history drives charts, and an
    estimate would distort the trend.
    """
    rows = (await conn.execute(text(
        "SELECT parse_status::text AS st, count(*) AS c FROM channel_queue GROUP BY st"
    ))).all()
    queue = {st: int(c) for st, c in rows}
    queue_total = sum(queue.values())
    done = queue.get("done_depth", 0)

    out: dict = {
        "queue": {
            "new": queue.get("new", 0), "in_progress": queue.get("in_progress", 0),
            "done_depth": done, "restricted_skip": queue.get("restricted_skip", 0),
            "error": queue.get("error", 0),
        },
        "coverage_pct": round(done / queue_total * 100, 1) if queue_total else 0.0,
    }
    if not full:
        return out

    out["posts"] = int(await _scalar(conn, "SELECT count(*) FROM posts") or 0)
    out["posts_1h"] = int(await _scalar(
        conn, "SELECT count(*) FROM posts WHERE created_at >= now() - interval '1 hour'") or 0)
    out["total_users"] = int(await _scalar(conn, "SELECT count(*) FROM user_stats") or 0)
    out["total_searches"] = int(await _scalar(
        conn, f"SELECT count(*) FROM search_log WHERE {_exclude_admin_sql()}") or 0)
    return out


# ════════════════ Section 2 — Ingestion ═════════════════════════════════════

async def collect_ingest(conn) -> dict:
    """Ingestion rate, freshness of the newest post and disk usage."""
    posts_1h_total = int(await _scalar(
        conn, "SELECT count(*) FROM posts WHERE created_at >= now() - interval '1 hour'") or 0)
    latest_post = await _scalar(conn, "SELECT max(created_at) FROM posts")

    disk_path = _DISK_PATH if os.path.exists(_DISK_PATH) else "/"
    du = shutil.disk_usage(disk_path)
    disk = {"used_bytes": du.used, "total_bytes": du.total,
            "pct": round(du.used / du.total * 100, 1) if du.total else 0.0}

    return {"posts_1h_total": posts_1h_total,
            "latest_post_at": latest_post.isoformat() if latest_post is not None else None,
            "disk": disk}


# ════════════════ Section 3 — Users / demand ════════════════════════════════

async def collect_users(conn) -> dict:
    total_users = int(await _scalar(conn, "SELECT count(*) FROM user_stats") or 0)
    active_7d = int(await _scalar(
        conn, "SELECT count(*) FROM user_stats WHERE last_seen >= now() - interval '7 days'") or 0)
    total_requests = int(await _scalar(
        conn, "SELECT coalesce(sum(requests_count), 0) FROM user_stats") or 0)

    # Admin queries are excluded from every demand block.
    adm = _exclude_admin_sql()

    # Counters in one pass: total/empty plus the content-filter split.
    # adult_only IS TRUE = 18+ mode; IS NOT TRUE (false OR legacy NULL) = safe mode/unknown.
    row = (await conn.execute(text(
        f"SELECT count(*) total, "
        f"count(*) FILTER (WHERE has_results = false) empty, "
        f"count(*) FILTER (WHERE adult_only IS TRUE) adult, "
        f"count(*) FILTER (WHERE adult_only IS NOT TRUE) safe "
        f"FROM search_log WHERE {adm}"))).first()
    total_searches = int(row[0]) if row else 0
    empty_searches = int(row[1]) if row else 0
    searches_adult = int(row[2]) if row else 0
    searches_safe = int(row[3]) if row else 0

    top = [{"query": q, "count": int(c)} for q, c in (await conn.execute(text(
        f"SELECT query, count(*) c FROM search_log WHERE {adm} "
        f"GROUP BY query ORDER BY c DESC, query LIMIT 10"))).all()]
    top_empty = [{"query": q, "count": int(c)} for q, c in (await conn.execute(text(
        f"SELECT query, count(*) c FROM search_log WHERE has_results = false AND {adm} "
        f"GROUP BY query ORDER BY c DESC, query LIMIT 10"))).all()]

    # Feed of the last 20 queries in CHRONOLOGICAL order (newest first). Cheap:
    # backward index scan on ix_search_log_created_at + LIMIT 20 (NOT a full scan).
    recent = [{
        "query": q,
        "at": ts.isoformat() if ts is not None else None,
        "adult": bool(a) if a is not None else None,
        "has_results": bool(hr),
    } for q, ts, a, hr in (await conn.execute(text(
        f"SELECT query, created_at, adult_only, has_results FROM search_log "
        f"WHERE {adm} ORDER BY created_at DESC LIMIT 20"))).all()]

    return {"total_users": total_users, "active_7d": active_7d, "total_requests": total_requests,
            "total_searches": total_searches, "empty_searches": empty_searches,
            "searches_adult": searches_adult, "searches_safe": searches_safe,
            "top": top, "top_empty": top_empty, "recent": recent}


# ════════════════ Charts (retrospective, from created_at) ═══════════════════

async def collect_charts(conn) -> dict:
    posts_hourly = [{"t": h, "c": int(c)} for h, c in (await conn.execute(text(
        "SELECT to_char(date_trunc('hour', created_at), 'YYYY-MM-DD\"T\"HH24:00') h, count(*) c "
        "FROM posts WHERE created_at >= now() - interval '48 hours' GROUP BY 1 ORDER BY 1"))).all()]
    posts_daily = [{"t": d, "c": int(c)} for d, c in (await conn.execute(text(
        "SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') d, count(*) c "
        "FROM posts WHERE created_at >= now() - interval '14 days' GROUP BY 1 ORDER BY 1"))).all()]
    searches_daily = [{"t": d, "c": int(c), "e": int(e)} for d, c, e in (await conn.execute(text(
        "SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') d, count(*) c, "
        "count(*) FILTER (WHERE has_results = false) e "
        "FROM search_log WHERE created_at >= now() - interval '14 days' "
        f"AND {_exclude_admin_sql()} GROUP BY 1 ORDER BY 1"))).all()]
    users_daily = [{"t": d, "c": int(c)} for d, c in (await conn.execute(text(
        "SELECT to_char(date_trunc('day', first_seen), 'YYYY-MM-DD') d, count(*) c "
        "FROM user_stats WHERE first_seen >= now() - interval '14 days' GROUP BY 1 ORDER BY 1"))).all()]
    return {"posts_hourly": posts_hourly, "posts_daily": posts_daily,
            "searches_daily": searches_daily, "users_daily": users_daily}


# ════════════════ Snapshot history (the panel's own file, NOT the project DB) ══

def read_history() -> list:
    try:
        with open(_HIST_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-_HIST_MAX:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def append_snapshot(snap: dict) -> None:
    """Append a metrics snapshot to the panel's OWN file (its volume). Not a DB write."""
    try:
        d = os.path.dirname(_HIST_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(_HIST_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        # Periodic trimming so the file does not grow forever.
        try:
            with open(_HIST_PATH, encoding="utf-8") as f:
                lines = f.read().splitlines()
            if len(lines) > int(_HIST_MAX * 1.25):
                with open(_HIST_PATH, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines[-_HIST_MAX:]) + "\n")
        except Exception:
            pass
    except Exception:
        pass


# ════════════════ Alerts (early warning) ════════════════════════════════════

def compute_alerts(base: dict, ingest: dict, charts: dict) -> list:
    alerts: list = []

    # 1. Ingestion rate dropped sharply (current hour vs average of previous hours).
    ph = charts.get("posts_hourly", [])
    if len(ph) >= 6:
        prior = [x["c"] for x in ph[:-1]]            # without the current (partial) hour
        avg = sum(prior) / len(prior) if prior else 0
        recent = ingest.get("posts_1h_total", 0)
        if avg >= _AL_RATE_MIN_AVG and recent < avg * _AL_RATE_FRAC:
            alerts.append({"level": "warn", "code": "rate_drop",
                           "msg": f"Ingestion rate dropped: {recent}/h vs ~{round(avg)}/h average"})

    # 2. Errors in the ingestion queue are high.
    err = base.get("queue", {}).get("error", 0)
    if err >= _AL_ERROR_ABS:
        alerts.append({"level": "warn", "code": "error_high",
                       "msg": f"Queue errors = {err} (threshold {_AL_ERROR_ABS}) — check the causes"})

    return alerts


# ════════════════ Deep analytics (breakdowns), with a TTL cache ═════════════
# Heavier aggregates are cached (180 s by default) so that polling the panel
# does NOT recompute them every time. The queries themselves are light:
# GROUP BY over indexed columns (category/type/channel_id), few groups.

_ANALYTICS_TTL = float(os.getenv("PANEL_ANALYTICS_TTL_SEC", "180"))
_acache: dict[str, tuple[float, Any]] = {}


async def _cached(key: str, ttl: float, factory):
    now = time.time()
    c = _acache.get(key)
    if c and now - c[0] < ttl:
        return c[1]
    val = await factory()
    _acache[key] = (now, val)
    return val


async def _build_analytics(engine: AsyncEngine) -> dict:
    async with engine.connect() as conn:
        # ── A. Categories: one pass over channel_queue (small, indexed) ──
        catrows = (await conn.execute(text(
            "SELECT coalesce(category,'(no category)') cat, count(*) total, "
            " count(*) FILTER (WHERE parse_status='done_depth') done, "
            " count(*) FILTER (WHERE parse_status='restricted_skip') restr, "
            " count(*) FILTER (WHERE parse_status='error') err, "
            " count(*) FILTER (WHERE parse_status IN ('new','in_progress')) pending, "
            " count(*) FILTER (WHERE channel_id IS NOT NULL) resolved "
            "FROM channel_queue GROUP BY 1"))).all()
        # Posts per category (join posts→channel_queue on the channel_id index).
        cat_posts = {c: int(n) for c, n in (await conn.execute(text(
            "SELECT cq.category, count(*) FROM posts p "
            "JOIN channel_queue cq ON cq.channel_id = p.channel_id "
            "WHERE cq.category IS NOT NULL GROUP BY cq.category"))).all()}

        categories = []
        for cat, total, done, restr, err, pending, resolved in catrows:
            total = int(total)
            cov = round(int(done) / total * 100, 1) if total else 0.0
            lagging = total >= 5 and (cov < 25.0 or int(pending) > int(resolved))
            categories.append({
                "category": cat, "total": total, "resolved": int(resolved),
                "posts": int(cat_posts.get(cat, 0)), "done": int(done),
                "restricted": int(restr), "error": int(err), "pending": int(pending),
                "coverage_pct": cov, "lagging": lagging,
            })
        categories.sort(key=lambda x: x["posts"], reverse=True)

        # ── A. Media types across the whole database ──
        media_by = {t: int(c) for t, c in (await conn.execute(text(
            "SELECT type::text, count(*) FROM media GROUP BY type"))).all()}
        posts_total = int(await _scalar(conn, "SELECT count(*) FROM posts") or 0)
        with_media = int(await _scalar(conn, "SELECT count(DISTINCT post_id) FROM media") or 0)
        media = {
            "posts_total": posts_total, "with_media": with_media,
            "text_only": max(0, posts_total - with_media),
            "by_type": {
                "photo": media_by.get("photo", 0), "video": media_by.get("video", 0),
                "audio": media_by.get("audio", 0), "document": media_by.get("document", 0),
            },
        }

        # ── B. Demand (search_log is small). Admin queries are excluded. ──
        adm = _exclude_admin_sql()
        empty_daily = [{"t": d, "c": int(c), "e": int(e)} for d, c, e in (await conn.execute(text(
            "SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') d, count(*) c, "
            "count(*) FILTER (WHERE has_results=false) e "
            "FROM search_log WHERE created_at >= now() - interval '30 days' "
            f"AND {adm} GROUP BY 1 ORDER BY 1"))).all()]
        top = [{"query": q, "count": int(c), "ever": bool(a)} for q, c, a in (await conn.execute(text(
            f"SELECT query, count(*) c, bool_or(has_results) a FROM search_log WHERE {adm} "
            "GROUP BY query ORDER BY c DESC, query LIMIT 12"))).all()]
        top_empty = [{"query": q, "count": int(c)} for q, c in (await conn.execute(text(
            f"SELECT query, count(*) c FROM search_log WHERE has_results=false AND {adm} "
            "GROUP BY query ORDER BY c DESC, query LIMIT 12"))).all()]

    # ── C. Queue dynamics + forecast (from the snapshot history file, no DB) ──
    hist = read_history()
    queue_history = [{
        "ts": h.get("ts"), "done_depth": h.get("done_depth"), "coverage_pct": h.get("coverage_pct"),
        "new": h.get("new"), "in_progress": h.get("in_progress"),
        "restricted_skip": h.get("restricted_skip"), "error": h.get("queue_error"),
    } for h in hist if h.get("done_depth") is not None]

    forecast = _forecast(hist, categories)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "categories": categories, "media": media,
        "demand": {"empty_daily": empty_daily, "top": top, "top_empty": top_empty},
        "queue_history": queue_history, "forecast": forecast,
    }


def _forecast(hist: list, categories: list) -> dict:
    """Rough forecast based on the growth of done_depth in the snapshot history."""
    pts = [h for h in hist if h.get("done_depth") is not None and h.get("ts")]
    pending_total = sum(c["pending"] for c in categories)  # still to ingest (new + in_progress)
    if len(pts) < 2:
        return {"ready": False, "note": "history is accumulating — the estimate appears after a few snapshots",
                "pending_total": pending_total}
    try:
        t0 = datetime.fromisoformat(pts[0]["ts"]); t1 = datetime.fromisoformat(pts[-1]["ts"])
        dt_days = (t1 - t0).total_seconds() / 86400.0
        d_done = pts[-1]["done_depth"] - pts[0]["done_depth"]
    except Exception:
        return {"ready": False, "note": "not enough history", "pending_total": pending_total}
    if dt_days < 0.01 or d_done <= 0:
        return {"ready": False, "note": "not enough time/progress for an estimate",
                "pending_total": pending_total, "done_per_day": round(d_done / dt_days) if dt_days else 0}
    done_per_day = d_done / dt_days
    eta_days = round(pending_total / done_per_day, 1) if done_per_day > 0 else None
    return {"ready": True, "done_per_day": round(done_per_day), "pending_total": pending_total,
            "eta_days": eta_days, "window_days": round(dt_days, 2),
            "note": f"at ~{round(done_per_day)} channels/day to full depth"}


async def collect_analytics(engine: AsyncEngine) -> dict:
    """Deep analytics — cached for _ANALYTICS_TTL so the database is not loaded."""
    return await _cached("analytics", _ANALYTICS_TTL, lambda: _build_analytics(engine))


# ════════════════ Everything together ═══════════════════════════════════════

async def collect_all(engine: AsyncEngine) -> dict:
    async with engine.connect() as conn:
        base = await collect_base(conn)
        ingest = await collect_ingest(conn)
        users = await collect_users(conn)
        charts = await collect_charts(conn)
    history = read_history()
    alerts = compute_alerts(base, ingest, charts)
    return {"base": base, "ingest": ingest, "users": users, "charts": charts,
            "history": history, "alerts": alerts}
