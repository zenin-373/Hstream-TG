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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def user_dir(user_id: int) -> Path:
    p = DOWNLOAD_ROOT / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_cookies_path(user_id: int) -> Path:
    return COOKIES_DIR / f"{user_id}.txt"


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_series_caption(info: SeriesInfo, has_subs: bool = True) -> str:
    """Series-level rich caption (once per hentai, before episodes)."""
    title = info.title or "Unknown"
    lines = [f"<blockquote><b>📖 {html_escape(title)}</b></blockquote>"]
    if info.title_jp:
        lines.append(f"<i>{html_escape(info.title_jp)}</i>")
    lines.append("")

    meta: list[str] = []
    if info.year:
        meta.append(f"📅 Year: <code>{html_escape(info.year)}</code>")
    if info.status:
        meta.append(f"📊 Status: <b>{html_escape(info.status)}</b>")
    if info.episodes:
        meta.append(f"📑 Total Episodes: <code>{info.episodes}</code>")
    if info.tags:
        tags = ", ".join(info.tags[:8])
        meta.append(f"🏷️ Tags: {html_escape(tags)}")
    if info.studio:
        meta.append(f"🌐 Studio: {html_escape(info.studio)}")
    meta.append(
        "🗣 Language: Japanese + Eng subs" if has_subs else "🗣 Language: Japanese"
    )
    if meta:
        lines += ["", "<blockquote>" + "\n".join(meta) + "</blockquote>"]

    text = "\n".join(lines)
    return text[:1020] + "…" if len(text) > 1024 else text


def build_episode_caption(url: str, final_path: Path, has_subs: bool) -> str:
    token = url.rstrip("/").split("/")[-1]
    ep_match = re.search(r"-(\d+)$", token)
    ep_num = ep_match.group(1) if ep_match else "?"
    return (
        f"<b>Episode {html_escape(ep_num)}</b>\n"
        f"<code>{html_escape(final_path.name)}</code>\n"
        f"💾 {human_size(final_path.stat().st_size)}"
        + (" · Eng subs" if has_subs else "")
    )


async def progress_edit(status: Message, text: str) -> None:
    try:
        await status.edit_text(text, parse_mode=enums.ParseMode.HTML)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            await status.edit_text(text, parse_mode=enums.ParseMode.HTML)
        except RPCError:
            pass
    except RPCError:
        pass


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message) -> None:
    text = (
        "👋 <b>HStream-TG</b> <i>(wzgram / MTProto)</i>\n\n"
        "Send me one or more <b>hstream.moe episode links</b> and I will:\n"
        "• download the video (best quality)\n"
        "• try English .ass subtitles + remux to MKV\n"
        "• post <b>poster + series caption once</b> per hentai\n"
        "• leech each episode (up to ~2 GB via MTProto)\n\n"
        "<b>Commands</b>\n"
        "/start – this message\n"
        "/help – detailed help\n"
        "/cookies – upload cookies.txt\n"
        "/status – jobs & disk usage\n"
        "/clear – delete your temporary files\n\n"
        "⚠️ Only <b>single-episode</b> URLs\n"
        "(e.g. <code>https://hstream.moe/hentai/title-1</code>)"
    )
    await message.reply(text, parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("help"))
async def help_cmd(client: Client, message: Message) -> None:
    text = (
        "<b>How to use</b>\n\n"
        "1. (Optional) <code>/cookies</code> then send Netscape cookies.txt\n\n"
        "2. Paste one or more episode URLs.\n"
        "   Same series → one poster/caption, then all episodes.\n"
        "   Next series → new poster/caption, then its episodes.\n\n"
        "3. Large files (200 MB+) upload via <b>wzgram MTProto</b> "
        f"(soft limit <code>{MAX_FILE_MB:.0f} MB</code>).\n\n"
        "<b>Notes</b>\n"
        "• Series pages are not expanded — send each episode link.\n"
        "• Subtitles: page → player API → known CDN hosts.\n"
        "• Need <code>API_ID</code> + <code>API_HASH</code> from my.telegram.org\n"
    )
    await message.reply(text, parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("status"))
async def status_cmd(client: Client, message: Message) -> None:
    uid = message.from_user.id
    ud = user_dir(uid)
    total = sum(f.stat().st_size for f in ud.rglob("*") if f.is_file())
    files = list(ud.glob("*"))
    text = (
        f"👤 User <code>{uid}</code>\n"
        f"📂 Files in your folder: <b>{len(files)}</b>\n"
        f"💾 Size: <b>{human_size(total)}</b>\n"
        f"⚙️ Active jobs (global): <b>{len(active_jobs)}</b>\n"
        f"🍪 Cookies: {'✅ present' if user_cookies_path(uid).exists() else '❌ none'}\n"
        f"📦 Max upload: <b>{MAX_FILE_MB:.0f} MB</b> (wzgram MTProto)\n"
    )
    await message.reply(text, parse_mode=enums.ParseMode.HTML)


@app.on_message(filters.command("clear"))
async def clear_cmd(client: Client, message: Message) -> None:
    uid = message.from_user.id
    ud = user_dir(uid)
    removed = 0
    for f in ud.glob("*"):
        try:
            if f.is_file():
                f.unlink()
                removed += 1
            elif f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
                removed += 1
        except Exception:
            pass
    await message.reply(f"🧹 Cleared {removed} item(s) from your folder.")


@app.on_message(filters.command("cookies"))
async def cookies_cmd(client: Client, message: Message) -> None:
    await message.reply(
        "🍪 Send me a <b>Netscape cookies.txt</b> file as a document.\n\n"
        "How to get it:\n"
        "• Browser extension (e.g. “Get cookies.txt LOCALLY”)\n"
        "• Or export from DevTools → Application → Cookies\n\n"
        "The file will be stored only for your Telegram account.",
        parse_mode=enums.ParseMode.HTML,
    )
    (COOKIES_DIR / f".await_{message.from_user.id}").touch()


@app.on_message(filters.document)
async def handle_document(client: Client, message: Message) -> None:
    uid = message.from_user.id
    flag = COOKIES_DIR / f".await_{uid}"
    if not flag.exists():
        return

    doc: Document = message.document
    name = (doc.file_name or "").lower()
    if not name.endswith((".txt", ".cookies")):
        await message.reply("Please send a .txt cookies file.")
        return

    dest = user_cookies_path(uid)
    await message.download(file_name=str(dest))
    flag.unlink(missing_ok=True)
    size = dest.stat().st_size
    await message.reply(
        f"✅ Cookies saved ({human_size(size)}).\n"
        "They will be used for your future downloads."
    )


@app.on_message(filters.text & ~filters.command(["start", "help", "status", "clear", "cookies"]))
async def handle_text(client: Client, message: Message) -> None:
    text = (message.text or "").strip()
    urls = URL_RE.findall(text)
    if not urls:
        await message.reply(
            "No valid hstream.moe episode URLs found.\n"
            "Send links like:\n<code>https://hstream.moe/hentai/title-1</code>",
            parse_mode=enums.ParseMode.HTML,
        )
        return

    seen = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]
    uid = message.from_user.id

    if uid in active_jobs:
        await message.reply("⏳ You already have a job running. Please wait.")
        return

    active_jobs.add(uid)
    try:
        await process_urls(client, message, urls)
    finally:
        active_jobs.discard(uid)


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------
async def process_urls(client: Client, message: Message, urls: list[str]) -> None:
    """Group by series → poster+caption once → leech all episodes."""
    uid = message.from_user.id
    dest = user_dir(uid)
    cookies = user_cookies_path(uid)
    cookies_file = cookies if cookies.exists() else None

    groups: Dict[str, list[str]] = {}
    order: list[str] = []
    for u in urls:
        key = episode_url_to_series_url(u)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(u)

    total_eps = len(urls)
    total_series = len(order)
    status = await message.reply(
        f"🚀 Starting <b>{total_eps}</b> episode(s) across "
        f"<b>{total_series}</b> series…\n"
        f"Cookies: {'✅' if cookies_file else '❌'}\n"
        f"Upload engine: <b>wzgram</b> (up to {MAX_FILE_MB:.0f} MB)",
        parse_mode=enums.ParseMode.HTML,
    )

    loop = asyncio.get_running_loop()
    ep_global = 0

    for s_idx, series_key in enumerate(order, 1):
        series_urls = groups[series_key]

        def _scrape(_url=series_urls[0]):
            return scrape_series_info(_url, cookies_file=cookies_file)

        try:
            series_info: SeriesInfo = await loop.run_in_executor(executor, _scrape)
        except Exception as e:
            logger.warning("Series scrape failed for %s: %s", series_key, e)
            series_info = SeriesInfo(series_url=series_key)

        series_caption = build_series_caption(series_info, has_subs=True)
        title_label = series_info.title or series_key.rsplit("/", 1)[-1]

        await progress_edit(
            status,
            f"📚 Series <b>[{s_idx}/{total_series}]</b> "
            f"{html_escape(title_label)}\n"
            f"Episodes in this batch: <b>{len(series_urls)}</b>\n"
            f"Posting info card…",
        )

        try:
            if series_info.poster_url:
                await message.reply_photo(
                    photo=series_info.poster_url,
                    caption=series_caption,
                    parse_mode=enums.ParseMode.HTML,
                )
            else:
                await message.reply(
                    series_caption,
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        except Exception as e:
            logger.warning("Series info post failed: %s", e)
            try:
                await message.reply(
                    series_caption,
                    parse_mode=enums.ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

        for url in series_urls:
            ep_global += 1
            idx = ep_global

            def progress_cb(msg: str, _idx=idx, _total=total_eps, _url=url) -> None:
                text = (
                    f"📥 <b>[{_idx}/{_total}]</b>\n"
                    f"<code>{_url}</code>\n\n"
                    f"{msg}"
                )
                asyncio.run_coroutine_threadsafe(progress_edit(status, text), loop)

            try:
                final_path: Path = await loop.run_in_executor(
                    executor,
                    lambda _u=url: process_url(
                        _u,
                        dest,
                        cookies_file=cookies_file,
                        progress=progress_cb,
                    ),
                )
            except Exception as e:
                logger.exception("Failed %s", url)
                await progress_edit(
                    status,
                    f"❌ <b>[{idx}/{total_eps}]</b> failed\n<code>{url}</code>\n\n{e}",
                )
                continue

            size_mb = final_path.stat().st_size / (1024 * 1024)
            has_subs = final_path.suffix.lower() == ".mkv"
            ep_caption = build_episode_caption(url, final_path, has_subs)

            await progress_edit(
                status,
                f"✅ <b>[{idx}/{total_eps}]</b> ready – uploading…\n"
                f"<code>{final_path.name}</code>\n"
                f"Size: {human_size(final_path.stat().st_size)}",
            )

            if size_mb > MAX_FILE_MB:
                await message.reply(
                    f"📦 File exceeds configured max "
                    f"({size_mb:.1f} MB > {MAX_FILE_MB:.0f} MB).\n"
                    f"<code>{final_path}</code>\n{ep_caption}",
                    parse_mode=enums.ParseMode.HTML,
                )
                continue

            try:
                await message.reply_document(
                    document=str(final_path),
                    file_name=final_path.name,
                    caption=ep_caption,
                    parse_mode=enums.ParseMode.HTML,
                )
            except FloodWait as e:
                await asyncio.sleep(e.value)
                await message.reply_document(
                    document=str(final_path),
                    file_name=final_path.name,
                    caption=ep_caption,
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception as e:
                logger.exception("Upload failed %s", final_path)
                await message.reply(
                    f"⚠️ Upload failed: {e}\n<code>{final_path}</code>",
                    parse_mode=enums.ParseMode.HTML,
                )

            if not KEEP_FILES:
                pass

    await progress_edit(
        status,
        f"🎉 All done! Processed <b>{total_eps}</b> episode(s) "
        f"across <b>{total_series}</b> series.\n"
        "Use /status or /clear when finished.",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Checking dependencies…")
    ensure_dependencies()
    logger.info("Starting HStream-TG with wzgram (MTProto)…")
    app.run()
