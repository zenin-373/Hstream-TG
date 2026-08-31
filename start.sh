#!/bin/bash
set -euo pipefail

UPDATE_ON_START="${UPDATE_ON_START:-false}"
UPSTREAM_REPO="${UPSTREAM_REPO:-https://github.com/zenin-373/Hstream-TG.git}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"

update_from_upstream() {
  echo "[start] UPDATE_ON_START=true → syncing from ${UPSTREAM_REPO} (${UPSTREAM_BRANCH})"
  if ! command -v git >/dev/null 2>&1; then
    echo "[start] git not found – skipping upstream update"
    return 0
  fi

  TMP="$(mktemp -d)"
  cleanup() { rm -rf "$TMP"; }
  trap cleanup EXIT

  if ! git clone --depth 1 --branch "$UPSTREAM_BRANCH" "$UPSTREAM_REPO" "$TMP/repo"; then
    echo "[start] WARNING: git clone failed – starting with current files"
    return 0
  fi

  for f in bot.py extractor.py thumb_utils.py requirements.txt; do
    if [ -f "$TMP/repo/$f" ]; then
      cp -f "$TMP/repo/$f" "./$f"
      echo "[start] updated $f"
    fi
  done

  if [ -f requirements.txt ]; then
    pip install -q -r requirements.txt || echo "[start] pip install skipped/failed"
  fi

  echo "[start] upstream sync done"
}

# case-insensitive true/1/yes
case "${UPDATE_ON_START}" in
  [Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]) update_from_upstream ;;
  *) echo "[start] UPDATE_ON_START not enabled – using image files as-is" ;;
esac

echo "[start] launching bot…"
exec python -u bot.py
