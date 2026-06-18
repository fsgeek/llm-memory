#!/bin/bash
set -e

GIT_ROOT=$(git rev-parse --show-toplevel)
OTS="$GIT_ROOT/.venv/bin/ots"
cd "$GIT_ROOT"

if [ ! -x "$OTS" ]; then
    echo "ots: client not found at $GIT_ROOT/.venv/bin/ots" >&2
    exit 1
fi

upgraded=0
for f in timestamps/*.ots; do
    [ -f "$f" ] || continue
    if "$OTS" upgrade "$f" 2>/dev/null; then
        echo "upgraded: $f"
        upgraded=$((upgraded + 1))
    else
        echo "pending:  $f"
    fi
done

if [ "$upgraded" -gt 0 ]; then
    git add timestamps/
    git commit --no-verify -m "ots: upgrade $upgraded timestamp(s)"
else
    echo "No timestamps ready to upgrade yet."
fi
