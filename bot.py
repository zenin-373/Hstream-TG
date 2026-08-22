#!/usr/bin/env python3
"""
HStream-TG – Telegram bot for hstream.moe downloads + subtitle remux.

Uses wzgram (Pyrogram MTProto) so files up to ~2 GB can be uploaded
(normal limit; Premium up to 4 GB on user clients).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.types import Message, Document
from pyrogram.errors import FloodWait, RPCError

from extractor import (
    process_url,
    ensure_dependencies,
    scrape_series_info,
    SeriesInfo,
    episode_url_to_series_url,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
# MTProto allows ~2 GB; soft safety cap in MB
MAX_FILE_MB = float(os.getenv("MAX_FILE_MB", "2000"))
DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "downloads")).resolve()
COOKIES_DIR = Path(os.getenv("COOKIES_DIR", "user_cookies")).resolve()
KEEP_FILES = os.getenv("KEEP_FILES", "false").lower() in {"1", "true", "yes"}
WORKERS = int(os.getenv("WORKERS", "2"))
SESSION_NAME = os.getenv("SESSION_NAME", "hstream_tg")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is required. Set it in .env or environment.")
if not API_ID or not API_HASH:
    raise SystemExit(
        "API_ID and API_HASH are required for wzgram/Pyrogram.\n"
        "Get them from https://my.telegram.org → API development tools."
    )

DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
COOKIES_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("hstream-tg")

executor = ThreadPoolExecutor(max_workers=WORKERS)
URL_RE = re.compile(r"https?://(?:www\.)?hstream\.moe/hentai/[\w\-]+/?", re.I)
active_jobs: set[int] = set()

app = Client(
    SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(Path(".").resolve()),
)
