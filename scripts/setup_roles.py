"""
Create/refresh the two database roles used by the monitoring panel.

  * teleseek_ro     — read-only analytics: LOGIN, default_transaction_read_only=on,
                      SELECT on every table (including tables created later).
  * teleseek_editor — the NARROW editor role. At the GRANT level (physically, not
                      only in code) it may change ONLY channels.is_adult and
                      channels.category, write the channel edit log, moderate
                      feedback (favourite flag + delete) and manage limits/premium
                      (the column-level grants for those tables live in the
                      baseline migration). No rights on posts, on the working
                      columns of channel_queue or on any other column of channels.

Passwords come from the environment (POSTGRES_RO_PASSWORD,
POSTGRES_EDITOR_PASSWORD) and are never stored in files. A role whose password
variable is empty is skipped. Idempotent: safe to run repeatedly; run it after
`alembic upgrade head` so that the grants cover every table.

Usage:
    python -m scripts.setup_roles        # DATABASE_URL must point at an owner/superuser
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

RO_ROLE = "teleseek_ro"
EDITOR_ROLE = "teleseek_editor"

EDITOR_GRANTS = [
    # channels: read the list (channel metadata is public) + change ONLY two columns.
    f"GRANT SELECT ON channels TO {EDITOR_ROLE}",
    f"GRANT UPDATE (is_adult, category) ON channels TO {EDITOR_ROLE}",
    # Edit log: write and read ONLY this table.
    f"GRANT SELECT, INSERT ON channel_edit_log TO {EDITOR_ROLE}",
    f"GRANT USAGE, SELECT ON SEQUENCE channel_edit_log_id_seq TO {EDITOR_ROLE}",
    # Queue: read-only (working columns stay non-writable).
    f"GRANT SELECT ON channel_queue TO {EDITOR_ROLE}",
    # Feedback: favourite toggle (column-level UPDATE) + delete; text/author are immutable.
    f"GRANT SELECT ON feedback TO {EDITOR_ROLE}",
    f"GRANT UPDATE (is_favorite) ON feedback TO {EDITOR_ROLE}",
    f"GRANT DELETE ON feedback TO {EDITOR_ROLE}",
]


def _literal(value: str) -> str:
    """Quote a value as an SQL string literal (ALTER ROLE does not accept bind parameters)."""
    return "'" + value.replace("'", "''") + "'"


async def _ensure_role(conn: AsyncConnection, role: str, password: str) -> None:
    exists = (await conn.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})).first()
    if not exists:
        await conn.execute(text(f"CREATE ROLE {role} LOGIN"))
    await conn.execute(text(
        f"ALTER ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {_literal(password)}"))
    db = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    await conn.execute(text(f'GRANT CONNECT ON DATABASE "{db}" TO {role}'))
    await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))


async def setup(conn: AsyncConnection) -> list[str]:
    done: list[str] = []
    ro_password = os.environ.get("POSTGRES_RO_PASSWORD", "")
    editor_password = os.environ.get("POSTGRES_EDITOR_PASSWORD", "")

    if ro_password:
        await _ensure_role(conn, RO_ROLE, ro_password)
        await conn.execute(text(f"ALTER ROLE {RO_ROLE} SET default_transaction_read_only = on"))
        await conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {RO_ROLE}"))
        await conn.execute(text(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {RO_ROLE}"))
        done.append(RO_ROLE)

    if editor_password:
        await _ensure_role(conn, EDITOR_ROLE, editor_password)
        for statement in EDITOR_GRANTS:
            await conn.execute(text(statement))
        done.append(EDITOR_ROLE)
    return done


async def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            roles = await setup(conn)
    finally:
        await engine.dispose()
    print("roles configured: " + (", ".join(roles) if roles else "none (no passwords set)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
