#!/usr/bin/env python3
"""
Aeon-style upstream sync for HStream-TG.

On Heroku restart (via start.sh) this pulls the latest code from UPSTREAM_REPO.
Set env vars:
  UPSTREAM_REPO=https://github.com/zenin-373/Hstream-TG.git
  UPSTREAM_BRANCH=main
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from subprocess import run

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s - %(message)s",
    level=logging.INFO,
    datefmt="%d-%b %I:%M:%S %p",
)
log = logging.getLogger("update")

UPSTREAM_REPO = (
    os.getenv("UPSTREAM_REPO", "").strip()
    or "https://github.com/zenin-373/Hstream-TG.git"
)
UPSTREAM_BRANCH = os.getenv("UPSTREAM_BRANCH", "").strip() or "main"

# Files that must not be wiped by git reset (session / user data)
PRESERVE = {
    ".env",
    "hstream_tg.session",
    "hstream_tg.session-journal",
}


def main() -> int:
    if not UPSTREAM_REPO:
        log.info("UPSTREAM_REPO empty – skip update")
        return 0

    log.info("Updating from %s (%s)", UPSTREAM_REPO, UPSTREAM_BRANCH)

    # Drop local .git so reset is always against the upstream remote
    if Path(".git").exists():
        run(["rm", "-rf", ".git"], check=False)

    cmd = (
        f"git init -q "
        f"&& git config --global user.email hstream@local "
        f"&& git config --global user.name hstream-tg "
        f"&& git add . "
        f"&& git commit -sm update -q || true "
        f"&& git remote add origin {UPSTREAM_REPO} "
        f"&& git fetch origin -q "
        f"&& git reset --hard origin/{UPSTREAM_BRANCH} -q"
    )
    result = run(cmd, shell=True, check=False)
    if result.returncode == 0:
        log.info("Successfully updated with latest commit from UPSTREAM_REPO")
    else:
        log.error(
            "Update failed (check UPSTREAM_REPO / network). Continuing with current files."
        )
        return 0  # do not block bot start

    # Optional: refresh deps if requirements changed
    if Path("requirements.txt").is_file():
        run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
            check=False,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
