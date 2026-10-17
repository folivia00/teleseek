#!/usr/bin/env bash
# Daily cleanup of search-session history.
#
# What it does: in `search_session` keeps ONE (the latest) row per user and deletes
# the rest. The latest row is the active result message — it must not be touched,
# otherwise the user's current buttons would stop working for no visible reason.
#
# What it does NOT do: it never touches `search_log` or `user_stats` — those are
# analytics, not interface state.
#
# Configuration (environment):
#   TELESEEK_DIR  directory with docker-compose.yml  (required)
#   LOG_DIR       where the log is written           (default: $TELESEEK_DIR/logs)
#
# Example crontab (after the nightly backup, UTC):
#   30 3 * * * TELESEEK_DIR=/srv/teleseek /srv/teleseek/scripts/cron/cleanup_sessions.sh
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

: "${TELESEEK_DIR:?set TELESEEK_DIR to the directory with docker-compose.yml}"
LOG_DIR="${LOG_DIR:-$TELESEEK_DIR/logs}"
LOG="$LOG_DIR/sessions_cleanup.log"
LOCK="${TMPDIR:-/tmp}/teleseek_sessions_cleanup.lock"

mkdir -p "$LOG_DIR"
cd "$TELESEEK_DIR" || { echo "$(date -Is) [error] no $TELESEEK_DIR" >>"$LOG"; exit 1; }

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) [skip] previous cleanup is still running" >>"$LOG"
  exit 0
fi

# Runs SQL from stdin with psql inside the postgres container.
psql_exec() {
  # shellcheck disable=SC2016  # the variables are expanded inside the container
  docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -A -F"|"'
}

echo "$(date -Is) [start] session cleanup (keep the latest per user)" >>"$LOG"
DELETED=$(psql_exec 2>>"$LOG" <<'SQL'
WITH ranked AS (
    SELECT id, row_number() OVER (
        PARTITION BY telegram_user_id ORDER BY created_at DESC, id DESC
    ) AS rn
    FROM search_session
), del AS (
    DELETE FROM search_session s USING ranked r
    WHERE s.id = r.id AND r.rn > 1
    RETURNING s.id
)
SELECT count(*) FROM del;
SQL
)
rc=$?
if [ $rc -ne 0 ] || [ -z "$DELETED" ]; then
  echo "$(date -Is) [error] cleanup failed (rc=$rc) — sessions NOT changed" >>"$LOG"
  exit "${rc:-1}"
fi

OUT=$(echo "SELECT count(*), count(DISTINCT telegram_user_id) FROM search_session;" | psql_exec 2>>"$LOG")
LEFT=$(echo "$OUT" | cut -d"|" -f1 | tr -d " ")
USERS=$(echo "$OUT" | cut -d"|" -f2 | tr -d " ")
echo "$(date -Is) [done] deleted=$(echo "$DELETED" | tr -d ' '), left=$LEFT (one for each of $USERS users)" >>"$LOG"
if [ "$LEFT" != "$USERS" ]; then
  echo "$(date -Is) [warn] $LEFT rows for $USERS users — expected them to be equal" >>"$LOG"
fi
