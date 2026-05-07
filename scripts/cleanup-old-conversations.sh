#!/bin/bash
# Rolling 30-day expiry of agent conversation archives.
# Deletes any groups/*/conversations/*-conversation-*.md whose mtime is
# older than 30 days. Pattern is intentionally specific so we can never
# hit anything outside the archived-transcript naming convention.
#
# conversations/ is write-only from agent-runner: full transcripts are
# archived there before compaction, nothing reads them back. Safe to
# delete on a rolling basis.
#
# Cron-driven (daily). Idempotent. Safe to run concurrently (flock).

set -euo pipefail

ROOT="/home/aurellian/nanoclaw"
LOGFILE="$ROOT/logs/cleanup-old-conversations.log"
LOCKFILE="/tmp/cleanup-old-conversations.lock"
RETENTION_DAYS=30

mkdir -p "$ROOT/logs"

exec 9>"$LOCKFILE"
flock -n 9 || { echo "[$(date -Iseconds)] another run in progress, skipping" >> "$LOGFILE"; exit 0; }

ts="$(date -Iseconds)"
echo "[$ts] cleanup-old-conversations.sh starting (retention=${RETENTION_DAYS}d)" >> "$LOGFILE"

deleted_count=0
while IFS= read -r f; do
    echo "  deleting: $f" >> "$LOGFILE"
    rm -f -- "$f"
    deleted_count=$((deleted_count + 1))
done < <(
    find "$ROOT/groups"/*/conversations \
        -maxdepth 1 -type f \
        -name '*-conversation-*.md' \
        -mtime +"$RETENTION_DAYS" \
        2>/dev/null
)

echo "[$ts] done; deleted=$deleted_count" >> "$LOGFILE"
