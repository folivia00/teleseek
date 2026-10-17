#!/usr/bin/env bash
# Periodic rebuild of channel topic keywords.
#
# Why: keywords are filled only by scripts/build_channel_keywords.py; the data
# pipeline does not set them, so new channels would not be found by the topic of
# their content until the next rebuild. This job restores coverage automatically
# (idempotent, takes a minute or two).
#
# Safety: flock (no overlap); a one-off container on the bot image
# (docker compose run --rm) — running services are not touched; the log is trimmed.
#
# Configuration (environment):
#   TELESEEK_DIR  directory with docker-compose.yml  (required)
#   LOG_DIR       where the log is written           (default: $TELESEEK_DIR/logs)
#
# Example crontab (nightly, UTC):
#   30 0 * * * TELESEEK_DIR=/srv/teleseek /srv/teleseek/scripts/cron/refresh_keywords.sh
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

: "${TELESEEK_DIR:?set TELESEEK_DIR to the directory with docker-compose.yml}"
LOG_DIR="${LOG_DIR:-$TELESEEK_DIR/logs}"
LOG="$LOG_DIR/keywords.log"
LOCK="${TMPDIR:-/tmp}/teleseek_keywords.lock"

mkdir -p "$LOG_DIR"
cd "$TELESEEK_DIR" || { echo "$(date -Is) [error] no $TELESEEK_DIR" >>"$LOG"; exit 1; }

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -Is) [skip] previous rebuild is still running" >>"$LOG"
  exit 0
fi

echo "$(date -Is) [start] keyword rebuild" >>"$LOG"
if docker compose run --rm -T --no-deps bot python -m scripts.build_channel_keywords >>"$LOG" 2>&1; then
  echo "$(date -Is) [done] ok" >>"$LOG"
else
  rc=$?
  echo "$(date -Is) [error] rc=$rc" >>"$LOG"
fi

# Keep the log bounded.
if [ -f "$LOG" ] && [ "$(wc -l <"$LOG")" -gt 2000 ]; then
  tail -n 2000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
