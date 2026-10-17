#!/usr/bin/env bash
# Retention for interaction telemetry: deletes `interaction_log` rows older than
# INTERACTION_RETENTION_DAYS.
#
# Why retention: the data is collected to make decisions (page limits, filter
# popularity), not to be kept forever. Half a year is enough to see seasonality and
# to compare behaviour before/after a change, while the table stays small.
#
# What it does NOT do: it never touches search_log, user_stats, user_subscription or
# admin_action_log.
#
# Configuration (environment):
#   TELESEEK_DIR                directory with docker-compose.yml  (required)
#   LOG_DIR                     where the log is written           (default: $TELESEEK_DIR/logs)
#   INTERACTION_RETENTION_DAYS  rows older than this are deleted   (default: 180)
#
# Example crontab (weekly, UTC):
#   0 4 * * 0 TELESEEK_DIR=/srv/teleseek /srv/teleseek/scripts/cron/cleanup_interactions.sh
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

: "${TELESEEK_DIR:?set TELESEEK_DIR to the directory with docker-compose.yml}"
LOG_DIR="${LOG_DIR:-$TELESEEK_DIR/logs}"
RETENTION_DAYS="${INTERACTION_RETENTION_DAYS:-180}"
LOG="$LOG_DIR/interactions_cleanup.log"
LOCK="${TMPDIR:-/tmp}/teleseek_interactions_cleanup.lock"

mkdir -p "$LOG_DIR"
case "$RETENTION_DAYS" in
  ''|*[!0-9]*) echo "$(date -Is) [error] INTERACTION_RETENTION_DAYS must be an integer" >>"$LOG"; exit 1 ;;
esac
cd "$TELESEEK_DIR" || { echo "$(date -Is) [error] no $TELESEEK_DIR" >>"$LOG"; exit 1; }

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) [skip] previous cleanup is still running" >>"$LOG"
  exit 0
fi

# Runs SQL from stdin with psql inside the postgres container.
psql_exec() {
  # shellcheck disable=SC2016  # the variables are expanded inside the container
  docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -A'
}

echo "$(date -Is) [start] deleting interaction_log rows older than ${RETENTION_DAYS} days" >>"$LOG"
DELETED=$(psql_exec 2>>"$LOG" <<SQL
WITH del AS (
    DELETE FROM interaction_log
    WHERE created_at < now() - make_interval(days => ${RETENTION_DAYS})
    RETURNING id
)
SELECT count(*) FROM del;
SQL
)
rc=$?
if [ $rc -ne 0 ] || [ -z "$DELETED" ]; then
  echo "$(date -Is) [error] cleanup failed (rc=$rc) — data NOT changed" >>"$LOG"
  exit "${rc:-1}"
fi
LEFT=$(echo "SELECT count(*) FROM interaction_log;" | psql_exec 2>>"$LOG" | tr -d " ")
echo "$(date -Is) [done] deleted=$(echo "$DELETED" | tr -d ' '), left=${LEFT}" >>"$LOG"
