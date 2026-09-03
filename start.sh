#!/bin/bash
# Always pull latest from UPSTREAM_REPO then start bot (Aeon-style).
set -euo pipefail

export UPSTREAM_REPO="${UPSTREAM_REPO:-https://github.com/zenin-373/Hstream-TG.git}"
export UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"

echo "[start] UPSTREAM_REPO=${UPSTREAM_REPO}"
echo "[start] UPSTREAM_BRANCH=${UPSTREAM_BRANCH}"

if command -v git >/dev/null 2>&1 && [ -f update.py ]; then
  echo "[start] running update.py…"
  python update.py || echo "[start] update.py failed – continuing"
else
  echo "[start] skip upstream update (no git or update.py)"
fi

echo "[start] launching bot…"
exec python -u bot.py
