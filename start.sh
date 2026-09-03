#!/bin/bash
# Heroku / Docker entrypoint – Aeon-style: always pull UPSTREAM then start bot.
set -euo pipefail

export UPSTREAM_REPO="${UPSTREAM_REPO:-https://github.com/zenin-373/Hstream-TG.git}"
export UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"

echo "[start] UPSTREAM_REPO=${UPSTREAM_REPO}"
echo "[start] UPSTREAM_BRANCH=${UPSTREAM_BRANCH}"

if command -v git >/dev/null 2>&1; then
  echo "[start] running update.py…"
  python update.py || echo "[start] update.py failed – continuing"
else
  echo "[start] git not installed – skip upstream update"
fi

echo "[start] launching bot…"
exec python -u bot.py
