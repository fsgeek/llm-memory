#!/bin/bash
# Install the nightly store backup (scripts/backup-store.sh) as a systemd user
# timer on this machine. Run on the backup host (wam-nuc), not on the machine
# that hosts the primary. Idempotent. Creates the restic password file if it
# does not exist — back that file up somewhere else: without it the remote
# backups cannot be read. Needs: arangodump/arangorestore, restic, an SSH key
# that reaches the restic host, and a local ArangoDB holding the llm_memory
# database and user (see spec). Lingering must be enabled for the timer to
# run without a login: sudo loginctl enable-linger $USER
set -e
GIT_ROOT=$(git rev-parse --show-toplevel)
for tool in arangodump arangorestore restic; do
  command -v "$tool" >/dev/null || { echo "FATAL: $tool not installed" >&2; exit 1; }
done
[ -x "$GIT_ROOT/.venv/bin/python" ] || { echo "FATAL: .venv missing. Run: uv sync" >&2; exit 1; }

PWFILE="$HOME/.config/llm-memory/restic-password"
if [ ! -s "$PWFILE" ]; then
  mkdir -p "$(dirname "$PWFILE")"
  (umask 077; openssl rand -base64 32 > "$PWFILE")
  echo "created restic password file $PWFILE — copy it somewhere safe; without it the remote backups are unreadable"
fi

UNITS="$HOME/.config/systemd/user"
mkdir -p "$UNITS"
cat > "$UNITS/llm-memory-backup.service" <<EOF
[Unit]
Description=Nightly backup of the llm-memory episodic store (dump, replica restore, restic)

[Service]
Type=oneshot
ExecStart=$GIT_ROOT/scripts/backup-store.sh
EOF
cat > "$UNITS/llm-memory-backup.timer" <<'EOF'
[Unit]
Description=Run llm-memory-backup nightly

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
RandomizedDelaySec=10min

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now llm-memory-backup.timer
systemctl --user list-timers llm-memory-backup.timer --no-pager | head -2
[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = "yes" ] || \
  echo "WARNING: lingering is off; the timer only runs while you are logged in. Fix: sudo loginctl enable-linger $USER"
echo "installed. First run now:  systemctl --user start llm-memory-backup.service && journalctl --user -u llm-memory-backup -n 20"
