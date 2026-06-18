#!/bin/bash
set -e

GIT_ROOT=$(git rev-parse --show-toplevel)
cd "$GIT_ROOT"

echo "Installing opentimestamps-client..."
uv pip install opentimestamps-client

echo "Installing git hooks..."
HOOK="$GIT_ROOT/.git/hooks/post-commit"
cat > "$HOOK" << 'EOF'
#!/bin/bash
exec "$(git rev-parse --show-toplevel)/scripts/hooks/post-commit" "$@"
EOF
chmod +x "$HOOK"

echo "Done. Run 'scripts/ots-upgrade.sh' a few hours after committing to anchor timestamps to Bitcoin."
