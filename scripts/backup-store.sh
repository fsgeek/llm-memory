#!/bin/bash
# Nightly backup of the episodic store. Runs on a machine other than the one
# that hosts the primary, so losing that machine does not also lose the job.
# Three tiers, each fail-loud:
#   1. arangodump the primary (config/db-config.ini) into $BACKUP_ROOT/<UTC date>
#   2. arangorestore that dump into the local ArangoDB — a warm replica, and the
#      database the primary will one day move to
#   3. restic backup $BACKUP_ROOT to the remote repository (encrypted,
#      deduplicated), then apply the retention policy
# Then verify: the replica's episode count equals the primary's. Local dump
# directories older than 30 days are removed unless dated the 1st of a month.
set -euo pipefail
GIT_ROOT=$(cd "$(dirname "$0")/.." && pwd)
CFG="$GIT_ROOT/config/db-config.ini"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/backups/llm-memory}"
REPLICA_ENDPOINT="${REPLICA_ENDPOINT:-tcp://127.0.0.1:8529}"
export RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-sftp:activitycontext.work:backups/llm-memory-restic}"
export RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE:-$HOME/.config/llm-memory/restic-password}"

read -r HOST PORT DB USER PW < <(python3 -c "
import configparser; c = configparser.ConfigParser(); c.read('$CFG'); d = c['database']
print(d['host'], d['port'], d['database'], d['user_name'], d['user_password'])")
PRIMARY="tcp://$HOST:$PORT"
DAY=$(date -u +%F)
DIR="$BACKUP_ROOT/$DAY"
mkdir -p "$DIR"

echo "[1/4] dump $DB from $PRIMARY -> $DIR"
arangodump --server.endpoint "$PRIMARY" --server.username "$USER" --server.password "$PW" \
  --server.database "$DB" --output-directory "$DIR" --compress-output true --overwrite true \
  --log.level warning

echo "[2/4] restore into replica $REPLICA_ENDPOINT"
arangorestore --server.endpoint "$REPLICA_ENDPOINT" --server.username "$USER" --server.password "$PW" \
  --server.database "$DB" --input-directory "$DIR" --overwrite true --log.level warning

echo "[3/4] verify counts"
PYTHONPATH="$GIT_ROOT" "$GIT_ROOT/.venv/bin/python" - "$HOST" "$PORT" "$DB" "$USER" "$PW" "$REPLICA_ENDPOINT" <<'EOF'
import sys
from arango import ArangoClient
host, port, db, user, pw, replica = sys.argv[1:]
rhost = replica.split("://", 1)[1]
n1 = ArangoClient(hosts=f"http://{host}:{port}").db(db, username=user, password=pw).collection("episodes").count()
n2 = ArangoClient(hosts=f"http://{rhost}").db(db, username=user, password=pw).collection("episodes").count()
print(f"primary {n1} episodes, replica {n2}")
if n1 != n2:
    raise SystemExit("VERIFY FAILED: replica count differs from primary")
EOF

echo "[4/4] restic -> $RESTIC_REPOSITORY"
restic snapshots --quiet >/dev/null 2>&1 || restic init
restic backup "$BACKUP_ROOT" --tag llm-memory --quiet
restic forget --tag llm-memory --keep-daily 30 --keep-monthly 24 --prune --quiet

# Local retention: 30 days of dailies, plus the 1st of every month.
find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20??-??-??' -mtime +30 ! -name '*-01' \
  -exec rm -rf {} + 2>/dev/null || true
echo "done $DAY: $(du -sh "$DIR" | cut -f1) local, $(ls -d "$BACKUP_ROOT"/20* | wc -l) dumps kept"
