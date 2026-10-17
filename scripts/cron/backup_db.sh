#!/usr/bin/env bash
# Daily compressed PostgreSQL backup with rotation.
#
# Why: the database holds user-facing state (users, search analytics, feedback);
# a nightly dump with a short rotation window is the minimum safety net.
#
# Safety:
#   * flock — never overlaps with itself;
#   * pg_dump -Fc (custom, compressed) runs inside the postgres container and is
#     streamed to the host; running containers are not touched, and pg_dump reads
#     a consistent snapshot without blocking writers;
#   * the dump is written to a .part file and renamed only on success;
#   * chmod 600 — the dump contains user data;
#   * a free-space warning before the dump.
#
# Configuration (environment):
#   TELESEEK_DIR        directory with docker-compose.yml   (required)
#   BACKUP_DIR          where dumps are stored              (default: $TELESEEK_DIR/backups)
#   LOG_DIR             where the log is written            (default: $TELESEEK_DIR/logs)
#   BACKUP_KEEP         number of dumps to keep             (default: 7)
#   BACKUP_MIN_FREE_MB  warn when free space is below this  (default: 3000)
# The database name and user come from the postgres container's own environment.
#
# Example crontab (low-traffic window, UTC):
#   30 2 * * * TELESEEK_DIR=/srv/teleseek /srv/teleseek/scripts/cron/backup_db.sh
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

: "${TELESEEK_DIR:?set TELESEEK_DIR to the directory with docker-compose.yml}"
BACKUP_DIR="${BACKUP_DIR:-$TELESEEK_DIR/backups}"
LOG_DIR="${LOG_DIR:-$TELESEEK_DIR/logs}"
KEEP="${BACKUP_KEEP:-7}"
MIN_FREE_MB="${BACKUP_MIN_FREE_MB:-3000}"
LOG="$LOG_DIR/backup.log"
LOCK="${TMPDIR:-/tmp}/teleseek_backup.lock"

mkdir -p "$LOG_DIR" "$BACKUP_DIR"
cd "$TELESEEK_DIR" || { echo "$(date -Is) [error] no $TELESEEK_DIR" >>"$LOG"; exit 1; }

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) [skip] previous backup is still running" >>"$LOG"
  exit 0
fi

FREE_MB=$(df -Pm "$BACKUP_DIR" | awk 'NR==2 {print $4}')
if [ "${FREE_MB:-0}" -lt "$MIN_FREE_MB" ]; then
  echo "$(date -Is) [warn] low disk space: ${FREE_MB} MB free < ${MIN_FREE_MB} MB — trying anyway, check disk/rotation" >>"$LOG"
fi

TS=$(date -u +%Y-%m-%d_%H%M)
OUT="$BACKUP_DIR/teleseek_${TS}.dump"
TMP="${OUT}.part"

echo "$(date -Is) [start] dump -> $OUT (${FREE_MB} MB free)" >>"$LOG"
# shellcheck disable=SC2016  # the variables are expanded inside the container
if docker compose exec -T postgres sh -c \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-privileges' \
    > "$TMP" 2>>"$LOG"; then
  chmod 600 "$TMP"
  mv "$TMP" "$OUT"
  echo "$(date -Is) [done] ok, size $(du -h "$OUT" | cut -f1)" >>"$LOG"
else
  rc=$?
  rm -f "$TMP"
  echo "$(date -Is) [error] pg_dump failed (rc=$rc) — no dump created" >>"$LOG"
  exit "$rc"
fi

# Rotation: keep the newest $KEEP dumps.
mapfile -t OLD < <(ls -1t "$BACKUP_DIR"/teleseek_*.dump 2>/dev/null | tail -n +$((KEEP + 1)))
for f in "${OLD[@]}"; do
  rm -f "$f" && echo "$(date -Is) [rotate] removed $(basename "$f")" >>"$LOG"
done
echo "$(date -Is) [end] dumps kept: $(ls -1 "$BACKUP_DIR"/teleseek_*.dump 2>/dev/null | wc -l)" >>"$LOG"
