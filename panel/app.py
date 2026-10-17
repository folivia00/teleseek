"""
TeleSeek — monitoring panel.

FastAPI application serving:
  * `/`            — the dashboard HTML shell (server-rendered);
  * `/api/metrics` — JSON with live metrics (read from the database via a TTL cache);
  * `/api/analytics`, `/api/editor/*`, `/api/feedback/*`, `/api/limits/*` — tab data/actions;
  * `/healthz`     — liveness probe.

Access: the port is published on the host's 127.0.0.1 only (see docker-compose),
like Adminer/postgres. It is not reachable from outside — use an SSH tunnel.

Database access uses a read-only role (`PANEL_DATABASE_URL`). Writes (editor,
feedback moderation, limits) go through a separate narrow role
(`PANEL_EDITOR_DATABASE_URL`) whose permissions are restricted by column-level
GRANTs in the database.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.requests import Request

import editor
import feedback as feedback_mod
import limits as limits_mod
import metrics

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("panel")

DATABASE_URL = os.environ["PANEL_DATABASE_URL"]
PROJECT_NAME = os.getenv("PANEL_PROJECT_NAME", "TeleSeek")

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

app = FastAPI(title="TeleSeek monitoring panel", docs_url=None, redoc_url=None)

# One shared engine per process. The pool is small and connections are short.
# Read-only (role teleseek_ro).
engine = create_async_engine(DATABASE_URL, pool_size=3, max_overflow=2, pool_pre_ping=True)

# A separate engine for the EDITOR — the narrow role teleseek_editor, which at
# the database GRANT level may only UPDATE (is_adult, category) on channels and
# INSERT into the edit log (plus the feedback/limits operations listed in the
# schema migration). If PANEL_EDITOR_DATABASE_URL is not set, the write tabs run
# in view-only mode (edits are rejected with 503); the dashboard keeps working.
EDITOR_DATABASE_URL = os.getenv("PANEL_EDITOR_DATABASE_URL", "").strip()
editor_engine = (
    create_async_engine(EDITOR_DATABASE_URL, pool_size=2, max_overflow=1, pool_pre_ping=True)
    if EDITOR_DATABASE_URL else None
)

# History snapshot interval. Snapshots go to the panel's OWN file (volume /data),
# NOT to the project database. Needed for the coverage-over-time chart
# (done_depth has no per-row history). Other charts are built retrospectively
# from created_at.
_SNAPSHOT_SEC = float(os.getenv("PANEL_SNAPSHOT_SEC", "300"))
# Every N-th snapshot tick also collects the slow counters (posts/posts_1h/users),
# each of which costs a full scan of posts. 6 × 300 s = every 30 minutes.
_SNAPSHOT_FULL_EVERY = max(1, int(os.getenv("PANEL_SNAPSHOT_FULL_EVERY", "6")))


async def _snapshot_loop() -> None:
    """Every _SNAPSHOT_SEC write a compact metrics snapshot to the history file.

    Collects only the MINIMAL set (`collect_snapshot_min`) — exactly the fields
    written to the history, so the loop stays cheap around the clock.
    """
    # First snapshot almost immediately (so the chart is not empty after start).
    await asyncio.sleep(5)
    tick = 0
    while True:
        try:
            # Every tick — the cheap queue numbers (coverage_pct/done_depth); slow
            # counters — once per _SNAPSHOT_FULL_EVERY ticks (see collect_snapshot_min).
            full = (tick % _SNAPSHOT_FULL_EVERY) == 0
            async with engine.connect() as conn:
                snap = await metrics.collect_snapshot_min(conn, full=full)
            q = snap["queue"]
            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "done_depth": q["done_depth"],
                "new": q["new"], "in_progress": q["in_progress"],
                "restricted_skip": q["restricted_skip"],
                "coverage_pct": snap["coverage_pct"],
                "queue_error": q["error"],
            }
            # Slow series are written on "full" ticks only — history readers already
            # tolerate missing fields (filters like `.get(...) is not None`).
            for key in ("posts", "posts_1h", "total_searches", "total_users"):
                if key in snap:
                    row[key] = snap[key]
            metrics.append_snapshot(row)
            tick += 1
        except Exception:
            logger.exception("History snapshot failed")
        await asyncio.sleep(_SNAPSHOT_SEC)


# ─── API response cache ──────────────────────────────────────────────────────
# The dashboard polls /api/metrics every 30 s, while a full collection costs
# seconds of database work — with a tab left open that would be a large share
# of database time and would evict the search working set from cache.
# TTL cache + "stale-while-revalidate":
#   * request with a fresh cache   → instant answer, the database is not touched;
#   * request with a stale cache   → previous data is returned IMMEDIATELY and the
#                                    refresh runs in the background (the user never
#                                    waits for a collection);
#   * the very first request       → computed synchronously (once after start).
# No races: single-flight via a refresh flag — concurrent polls do not spawn
# parallel collections. Freshness is NOT hidden: the data age goes into the
# response (`cache.age_sec`) and is shown in the UI, so nobody mistakes a
# five-minute-old number for real time.
_METRICS_TTL = float(os.getenv("PANEL_METRICS_TTL_SEC", "300"))
_ANALYTICS_TTL = float(os.getenv("PANEL_ANALYTICS_TTL_SEC", "900"))


class _TtlCache:
    """Async single-value cache: TTL + stale-while-revalidate + single-flight."""

    def __init__(self, ttl: float, name: str) -> None:
        self._ttl = ttl
        self._name = name
        self._value: Optional[dict] = None
        self._at: float = 0.0
        self._lock = asyncio.Lock()
        self._refreshing = False

    async def get(self, producer) -> tuple[dict, float]:
        """Return (data, age in seconds). Never blocks longer than the first collection."""
        now = time.time()
        if self._value is not None and (now - self._at) < self._ttl:
            return self._value, now - self._at

        if self._value is None:                       # cold start — compute synchronously
            async with self._lock:
                if self._value is None:
                    self._value = await producer()
                    self._at = time.time()
            return self._value, 0.0

        if not self._refreshing:                      # stale → refresh in the background
            self._refreshing = True
            asyncio.create_task(self._refresh(producer))
        return self._value, now - self._at

    async def _refresh(self, producer) -> None:
        try:
            value = await producer()
            self._value, self._at = value, time.time()
        except Exception:
            logger.exception("Background refresh of cache %s failed", self._name)
        finally:
            self._refreshing = False


_metrics_cache = _TtlCache(_METRICS_TTL, "metrics")
_analytics_cache = _TtlCache(_ANALYTICS_TTL, "analytics")


async def _warmup_caches() -> None:
    """Warm the metrics cache right after start.

    Without warm-up the FIRST request after a panel restart pays for the full
    collection — the only case where a user would wait. Warm-up moves that
    collection into the background before anyone arrives; afterwards every
    request is served from the cache.
    """
    try:
        await _metrics_cache.get(lambda: metrics.collect_all(engine))
    except Exception:
        logger.exception("Metrics cache warm-up failed (non-critical)")
    try:
        await _analytics_cache.get(lambda: metrics.collect_analytics(engine))
    except Exception:
        logger.exception("Analytics cache warm-up failed (non-critical)")


@app.on_event("startup")
async def _startup() -> None:
    app.state.snapshot_task = asyncio.create_task(_snapshot_loop())
    app.state.warmup_task = asyncio.create_task(_warmup_caches())


@app.on_event("shutdown")
async def _shutdown() -> None:
    for attr in ("snapshot_task", "warmup_task"):
        task = getattr(app.state, attr, None)
        if task is not None:
            task.cancel()
    await engine.dispose()
    if editor_engine is not None:
        await editor_engine.dispose()


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/api/metrics")
async def api_metrics() -> JSONResponse:
    """Database metrics (read-only) through the TTL cache: the answer is instant and
    collection runs at most once per TTL, in the background. The data age is
    returned in `data.cache` and shown in the UI."""
    try:
        data, age = await _metrics_cache.get(lambda: metrics.collect_all(engine))
        data = {**data, "cache": {"age_sec": int(age), "ttl_sec": int(_METRICS_TTL)}}
        return JSONResponse({"ok": True, "data": data})
    except Exception as e:  # noqa: BLE001 — a single metric must not take the panel down
        logger.exception("Metrics collection failed")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/analytics")
async def api_analytics() -> JSONResponse:
    """Deep analytics (read-only). Changes slowly → a longer TTL than the metrics."""
    try:
        data, age = await _analytics_cache.get(lambda: metrics.collect_analytics(engine))
        data = {**data, "cache": {"age_sec": int(age), "ttl_sec": int(_ANALYTICS_TTL)}}
        return JSONResponse({"ok": True, "data": data})
    except Exception as e:  # noqa: BLE001
        logger.exception("Analytics collection failed")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


# ─── Channel labelling editor (content filter flag / category) ───────────────
# Reads go through the read-only engine (teleseek_ro). Writes go through
# editor_engine (the narrow role). Access is the same as for the whole panel:
# localhost-only via an SSH tunnel.

@app.get("/api/editor/channels")
async def api_editor_channels(q: str = "", adult: int = 1, category: str = "",
                              has: str = "", page: int = 0, page_size: int = 30) -> JSONResponse:
    # has: "" = all, "yes" = only with posts, "no" = only without posts.
    has_posts = True if has == "yes" else False if has == "no" else None
    try:
        data = await editor.list_channels(
            engine, q=(q or None), only_adult=bool(adult),
            category=(category or None), has_posts=has_posts,
            page=page, page_size=page_size)
        return JSONResponse({"ok": True, "data": data, "editable": editor_engine is not None})
    except Exception as e:  # noqa: BLE001
        logger.exception("editor list")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/editor/categories")
async def api_editor_categories() -> JSONResponse:
    try:
        return JSONResponse({"ok": True, "data": await editor.list_categories(engine)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/editor/log")
async def api_editor_log(channel_id: int = -1, limit: int = 20) -> JSONResponse:
    if editor_engine is None:
        return JSONResponse({"ok": True, "data": []})   # no editor role — no log
    try:
        cid = channel_id if channel_id >= 0 else None
        return JSONResponse({"ok": True, "data": await editor.recent_log(
            editor_engine, channel_id=cid, limit=limit)})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/editor/channel/{channel_id}")
async def api_editor_edit(channel_id: int, payload: dict) -> JSONResponse:
    """Apply ONE edit (is_adult|category) through the narrow role. Body: {field, value}."""
    if editor_engine is None:
        return JSONResponse(
            {"ok": False, "error": "Editor is disabled: PANEL_EDITOR_DATABASE_URL is not set"},
            status_code=503)
    field = (payload or {}).get("field", "")
    value = (payload or {}).get("value")
    try:
        res = await editor.apply_edit(editor_engine, engine,
                                      channel_id=channel_id, field=field, value=value)
        logger.info("EDITOR: channel id=%s %s: %s → %s",
                    res["channel_id"], res["field"], res["old"], res["new"])
        return JSONResponse({"ok": True, "data": res})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        logger.exception("editor edit")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


# ─── User feedback ───────────────────────────────────────────────────────────
# The feed uses the read-only engine (teleseek_ro). Actions (delete/favourite)
# use editor_engine (narrow role: UPDATE (is_favorite) + DELETE on feedback).

@app.get("/api/feedback")
async def api_feedback(favorites: int = 0, page: int = 0, page_size: int = 30) -> JSONResponse:
    """Feedback feed (newest first, paginated, optional "favourites only" filter)."""
    try:
        data = await feedback_mod.list_feedback(
            engine, only_favorites=bool(favorites), page=page, page_size=page_size)
        return JSONResponse({"ok": True, "data": data, "editable": editor_engine is not None})
    except Exception as e:  # noqa: BLE001
        logger.exception("feedback list")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/feedback/{feedback_id}/favorite")
async def api_feedback_favorite(feedback_id: int, payload: dict) -> JSONResponse:
    """Favourite toggle. Body: {value: true|false}."""
    if editor_engine is None:
        return JSONResponse(
            {"ok": False, "error": "Actions are disabled: PANEL_EDITOR_DATABASE_URL is not set"},
            status_code=503)
    try:
        value = bool((payload or {}).get("value"))
        res = await feedback_mod.set_favorite(editor_engine, feedback_id=feedback_id, value=value)
        logger.info("FEEDBACK: id=%s favourite → %s", feedback_id, value)
        return JSONResponse({"ok": True, "data": res})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        logger.exception("feedback favorite")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/feedback/{feedback_id}/delete")
async def api_feedback_delete(feedback_id: int) -> JSONResponse:
    """Delete one feedback entry (confirmation happens in the UI)."""
    if editor_engine is None:
        return JSONResponse(
            {"ok": False, "error": "Actions are disabled: PANEL_EDITOR_DATABASE_URL is not set"},
            status_code=503)
    try:
        res = await feedback_mod.delete_feedback(editor_engine, feedback_id=feedback_id)
        logger.info("FEEDBACK: id=%s deleted", feedback_id)
        return JSONResponse({"ok": True, "data": res})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        logger.exception("feedback delete")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


# ─── "Limits & access" tab ───────────────────────────────────────────────────
# Edits go ONLY through the narrow editor role; reads go through the read-only
# role. Telemetry figures are cached (the panel must not hit the database on
# every render); settings are read directly — the table has three rows.
_limits_stats_cache = _TtlCache(float(os.getenv("PANEL_LIMITS_STATS_TTL_SEC", "300")), "limits_stats")


@app.get("/api/limits")
async def api_limits_state() -> JSONResponse:
    """Current limits, validation bounds and the premium summary."""
    try:
        data = await limits_mod.get_state(engine)
        return JSONResponse({"ok": True, "data": data, "editable": editor_engine is not None})
    except Exception as e:  # noqa: BLE001
        logger.exception("limits state")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/limits/setting")
async def api_limits_setting(payload: dict) -> JSONResponse:
    """Change a setting. Body: {key, value}. Bounds are validated in limits_mod."""
    if editor_engine is None:
        return JSONResponse(
            {"ok": False, "error": "Edits are disabled: PANEL_EDITOR_DATABASE_URL is not set"},
            status_code=503)
    try:
        p = payload or {}
        res = await limits_mod.update_setting(
            editor_engine, key=str(p.get("key", "")), value=p.get("value"))
        logger.info("LIMITS: %s: %s → %s", res["key"], res["old"], res["new"])
        return JSONResponse({"ok": True, "data": res})
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        logger.exception("limits setting")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/limits/user")
async def api_limits_user(uid: int) -> JSONResponse:
    """User card (activity + subscription status) — lookup by Telegram id."""
    try:
        return JSONResponse({"ok": True, "data": await limits_mod.find_user(engine, uid)})
    except Exception as e:  # noqa: BLE001
        logger.exception("limits user")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/limits/premium")
async def api_limits_premium(payload: dict) -> JSONResponse:
    """Grant/revoke premium. Body: {uid, action: grant|revoke, days?, forever?}."""
    if editor_engine is None:
        return JSONResponse(
            {"ok": False, "error": "Edits are disabled: PANEL_EDITOR_DATABASE_URL is not set"},
            status_code=503)
    try:
        p = payload or {}
        uid = int(p.get("uid"))
        if str(p.get("action")) == "revoke":
            res = await limits_mod.revoke_premium(editor_engine, telegram_user_id=uid)
            logger.info("PREMIUM: revoked for %s (rows: %s)", uid, res["revoked"])
        else:
            res = await limits_mod.grant_premium(
                editor_engine, telegram_user_id=uid,
                days=p.get("days"), forever=bool(p.get("forever")))
            logger.info("PREMIUM: granted to %s (%s)", uid, res["human"])
        return JSONResponse({"ok": True, "data": res})
    except (ValueError, TypeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        logger.exception("limits premium")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/limits/log")
async def api_limits_log(limit: int = 20) -> JSONResponse:
    """Panel action log (who, what, old → new)."""
    try:
        return JSONResponse({"ok": True, "data": await limits_mod.recent_log(engine, limit)})
    except Exception as e:  # noqa: BLE001
        logger.exception("limits log")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/limits/stats")
async def api_limits_stats(days: int = 14) -> JSONResponse:
    """Telemetry figures (paging depth, filters, actions) — through the TTL cache."""
    try:
        data, age = await _limits_stats_cache.get(lambda: limits_mod.usage_stats(engine, days))
        data = {**data, "cache": {"age_sec": int(age)}}
        return JSONResponse({"ok": True, "data": data})
    except Exception as e:  # noqa: BLE001
        logger.exception("limits stats")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    # no-store: without it, after the SSH tunnel dropped the browser rendered the
    # dashboard shell from its cache — "UI present, no numbers" looked like a
    # broken panel, although requests never reached the server. A fresh shell
    # every time: no tunnel → an honest connection error instead of a half-alive page.
    return templates.TemplateResponse(
        "index.html", {"request": request, "project_name": PROJECT_NAME},
        headers={"Cache-Control": "no-store"},
    )
