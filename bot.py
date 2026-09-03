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
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Optional

import requests
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

API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
MAX_FILE_MB = float(os.getenv("MAX_FILE_MB", "2000"))
DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "downloads")).resolve()
COOKIES_DIR = Path(os.getenv("COOKIES_DIR", "user_cookies")).resolve()
KEEP_FILES = os.getenv("KEEP_FILES", "false").lower() in {"1", "true", "yes"}
WORKERS = int(os.getenv("WORKERS", "2"))
SESSION_NAME = os.getenv("SESSION_NAME", "hstream_tg")
UPLOAD_CHANNEL = (os.getenv("UPLOAD_CHANNEL") or "").strip() or None
DUMP_CHANNEL = (os.getenv("DUMP_CHANNEL") or "").strip() or None

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


def user_dir(user_id: int) -> Path:
    p = DOWNLOAD_ROOT / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_cookies_path(user_id: int) -> Path:
    return COOKIES_DIR / f"{user_id}.txt"


def progress_bar(pct: float, width: int = 10) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return "●" * filled + "○" * (width - filled)


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def sys_stats_line() -> str:
    cpu = ram = disk = "?"
    try:
        load1, _, _ = os.getloadavg()
        cpu = f"{load1:.1f} load"
    except Exception:
        pass
    try:
        mem = Path("/proc/meminfo").read_text()
        vals = {}
        for line in mem.splitlines():
            if line.startswith(("MemTotal:", "MemAvailable:")):
                k, v, *_ = line.split()
                vals[k.rstrip(":")] = int(v)
        if "MemTotal" in vals and "MemAvailable" in vals:
            used = vals["MemTotal"] - vals["MemAvailable"]
            ram = f"{100.0 * used / vals['MemTotal']:.1f}%"
    except Exception:
        pass
    try:
        usage = shutil.disk_usage(str(DOWNLOAD_ROOT))
        free_gb = usage.free / (1024 ** 3)
        disk = f"{free_gb:.2f}GB free"
    except Exception:
        pass
    return f"CPU: {cpu} | RAM: {ram} | DISK: {disk}"


def html_escape(s: str) -> str:
    amp, lt, gt, quot = "&" + "amp;", "&" + "lt;", "&" + "gt;", "&" + "quot;"
    return (
        s.replace("&", amp)
        .replace("<", lt)
        .replace(">", gt)
        .replace('"', quot)
    )


def build_series_caption(info: SeriesInfo, has_subs: bool = True) -> str:
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


def episode_number_from_url(url: str) -> str:
    token = url.rstrip("/").split("/")[-1]
    ep_match = re.search(r"-(\d+)$", token)
    return ep_match.group(1) if ep_match else "?"


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:180] or "file"


def rename_episode_file(final_path: Path, anime_title: str, ep_num: str) -> Path:
    title = sanitize_filename(anime_title or final_path.stem)
    ext = final_path.suffix or ".mkv"
    new_name = f"{title} - {ep_num}{ext}"
    target = final_path.with_name(new_name)
    if target.resolve() == final_path.resolve():
        return final_path
    if target.exists():
        target.unlink()
    final_path.rename(target)
    return target


def build_episode_caption(anime_title: str, ep_num: str, final_path: Path, has_subs: bool) -> str:
    return html_escape(final_path.name)


def parse_chat_id(raw: Optional[str]):
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    if s.startswith("@"):
        return s
    try:
        return int(s)
    except ValueError:
        return s


UPLOAD_CHAT = parse_chat_id(UPLOAD_CHANNEL)
DUMP_CHAT = parse_chat_id(DUMP_CHANNEL)

from thumb_utils import (
    create_user_thumb,
    download_poster_thumb,
    resolve_doc_thumb,
    user_thumb_path,
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


async def send_photo_no_reply(client: Client, chat_id, photo: str, caption: str = "") -> None:
    await client.send_photo(
        chat_id=chat_id,
        photo=photo,
        caption=caption or None,
        parse_mode=enums.ParseMode.HTML,
    )


async def send_document_no_reply(
    client: Client,
    chat_id,
    document: str,
    file_name: str,
    caption: str,
    thumb: Optional[str] = None,
    progress=None,
) -> None:
    kwargs = dict(
        chat_id=chat_id,
        document=document,
        file_name=file_name,
        caption=caption,
        parse_mode=enums.ParseMode.HTML,
    )
    if thumb:
        kwargs["thumb"] = thumb
    if progress is not None:
        kwargs["progress"] = progress
    try:
        await client.send_document(**kwargs)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await client.send_document(**kwargs)
    except Exception:
        kwargs.pop("thumb", None)
        try:
            await client.send_document(**kwargs)
        except Exception:
            kwargs.pop("progress", None)
            await client.send_document(**kwargs)


def media_destinations(fallback_chat_id: int) -> list:
    dests = []
    primary = UPLOAD_CHAT if UPLOAD_CHAT is not None else fallback_chat_id
    dests.append(primary)
    if DUMP_CHAT is not None and DUMP_CHAT != primary:
        dests.append(DUMP_CHAT)
    return dests


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
        "/thumb – set custom leech thumbnail (Aeon style)\n"
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
        "1. (Optional) <code>/cookies</code> then send Netscape cookies.txt\n"
        "2. (Optional) <code>/thumb</code> then send a photo for custom leech thumb\n"
        "3. Paste one or more episode URLs.\n"
        "4. Large files upload via <b>wzgram MTProto</b> "
        f"(soft limit <code>{MAX_FILE_MB:.0f} MB</code>).\n"
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
        f"📂 Files: <b>{len(files)}</b>\n"
        f"💾 Size: <b>{human_size(total)}</b>\n"
        f"⚙️ Active jobs: <b>{len(active_jobs)}</b>\n"
        f"🍪 Cookies: {'✅' if user_cookies_path(uid).exists() else '❌'}\n"
        f"🖼 Thumb: {'✅' if user_thumb_path(uid).exists() else '❌'}\n"
        f"📦 Max upload: <b>{MAX_FILE_MB:.0f} MB</b>\n"
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
    await message.reply(f"🧹 Cleared {removed} item(s).")


@app.on_message(filters.command("cookies"))
async def cookies_cmd(client: Client, message: Message) -> None:
    await message.reply(
        "🍪 Send a <b>Netscape cookies.txt</b> as a document.",
        parse_mode=enums.ParseMode.HTML,
    )
    (COOKIES_DIR / f".await_{message.from_user.id}").touch()


@app.on_message(filters.command("thumb"))
async def thumb_cmd(client: Client, message: Message) -> None:
    uid = message.from_user.id
    path = user_thumb_path(uid)
    if message.reply_to_message and message.reply_to_message.photo:
        dl = await message.reply_to_message.download()
        out = create_user_thumb(Path(dl), uid)
        Path(dl).unlink(missing_ok=True)
        if out:
            await message.reply("✅ Custom thumbnail saved (used for all uploads).")
        else:
            await message.reply("❌ Failed to save thumbnail (need ffmpeg).")
        return
    (COOKIES_DIR / f".await_thumb_{uid}").touch()
    extra = "\nCurrent: ✅ set" if path.exists() else "\nCurrent: ❌ none"
    await message.reply(
        "🖼 Send a <b>photo</b> now to set your leech thumbnail."
        f"{extra}\n"
        "Same idea as Aeon <code>/settings → thumbnail</code>.",
        parse_mode=enums.ParseMode.HTML,
    )


@app.on_message(filters.photo)
async def handle_photo(client: Client, message: Message) -> None:
    uid = message.from_user.id
    flag = COOKIES_DIR / f".await_thumb_{uid}"
    if not flag.exists():
        return
    dl = await message.download()
    out = create_user_thumb(Path(dl), uid)
    Path(dl).unlink(missing_ok=True)
    flag.unlink(missing_ok=True)
    if out:
        await message.reply("✅ Custom thumbnail saved.")
    else:
        await message.reply("❌ Failed to save thumbnail.")


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
    await message.reply(f"✅ Cookies saved ({human_size(dest.stat().st_size)}).")


@app.on_message(filters.text & ~filters.command(["start", "help", "status", "clear", "cookies", "thumb"]))
async def handle_text(client: Client, message: Message) -> None:
    text = (message.text or "").strip()
    urls = URL_RE.findall(text)
    if not urls:
        await message.reply(
            "No valid hstream.moe episode URLs found.\n"
            "<code>https://hstream.moe/hentai/title-1</code>",
            parse_mode=enums.ParseMode.HTML,
        )
        return
    seen = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]
    uid = message.from_user.id
    if uid in active_jobs:
        await message.reply("⏳ You already have a job running.")
        return
    active_jobs.add(uid)
    try:
        await process_urls(client, message, urls)
    finally:
        active_jobs.discard(uid)


async def process_urls(client: Client, message: Message, urls: list[str]) -> None:
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
        f"🚀 Starting <b>{total_eps}</b> episode(s) across <b>{total_series}</b> series…\n"
        f"Cookies: {'✅' if cookies_file else '❌'}\n"
        f"Upload: <b>wzgram</b> (up to {MAX_FILE_MB:.0f} MB)",
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
        anime_title = series_info.title or title_label
        dest_chats = media_destinations(message.chat.id)

        series_thumb_dir = dest / "_thumbs" / series_key.rstrip("/").split("/")[-1]
        series_thumb: Optional[Path] = None
        if series_info.poster_url:
            series_thumb = await loop.run_in_executor(
                executor,
                lambda: download_poster_thumb(series_info.poster_url, series_thumb_dir),
            )

        await progress_edit(
            status,
            f"📚 Series <b>[{s_idx}/{total_series}]</b> {html_escape(title_label)}\n"
            f"Episodes: <b>{len(series_urls)}</b>",
        )

        for chat_id in dest_chats:
            try:
                if series_info.poster_url:
                    await send_photo_no_reply(client, chat_id, series_info.poster_url, series_caption)
                else:
                    await client.send_message(
                        chat_id=chat_id, text=series_caption,
                        parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True,
                    )
            except Exception as e:
                logger.warning("Series info post failed to %s: %s", chat_id, e)

        for url in series_urls:
            ep_global += 1
            idx = ep_global

            def progress_cb(msg: str, _idx=idx, _total=total_eps, _url=url) -> None:
                text = (
                    f"<b>[{_idx}/{_total}]</b>  <code>{html_escape(_url.split('/')[-1])}</code>\n"
                    f"{msg}\n"
                    f"<i>{sys_stats_line()}</i>"
                )
                asyncio.run_coroutine_threadsafe(progress_edit(status, text), loop)

            try:
                final_path: Path = await loop.run_in_executor(
                    executor,
                    lambda _u=url: process_url(_u, dest, cookies_file=cookies_file, progress=progress_cb),
                )
            except Exception as e:
                logger.exception("Failed %s", url)
                await progress_edit(status, f"❌ <b>[{idx}/{total_eps}]</b> failed\n<code>{url}</code>\n{e}")
                continue

            ep_num = episode_number_from_url(url)
            final_path = rename_episode_file(final_path, anime_title, ep_num)
            size_mb = final_path.stat().st_size / (1024 * 1024)
            has_subs = final_path.suffix.lower() == ".mkv"
            ep_caption = build_episode_caption(anime_title, ep_num, final_path, has_subs)

            await progress_edit(
                status,
                f"✅ <b>[{idx}/{total_eps}]</b> ready – uploading…\n"
                f"<code>{final_path.name}</code>\n"
                f"Size: {human_size(final_path.stat().st_size)}",
            )

            if size_mb > MAX_FILE_MB:
                await message.reply(
                    f"📦 File too large ({size_mb:.1f} MB > {MAX_FILE_MB:.0f} MB).\n{ep_caption}",
                    parse_mode=enums.ParseMode.HTML,
                )
                continue

            thumb_path = await loop.run_in_executor(
                executor,
                lambda: resolve_doc_thumb(
                    final_path, uid, series_thumb, series_thumb_dir
                ),
            )

            uploaded_ok = False
            for chat_id in dest_chats:
                last_up = [0.0]

                async def upload_progress(
                    current: int,
                    total: int,
                    _idx=idx,
                    _total=total_eps,
                    _name=final_path.name,
                ):
                    now = time.time()
                    if total and now - last_up[0] < 1.2 and current < total:
                        return
                    last_up[0] = now
                    pct = (100.0 * current / total) if total else 0.0
                    bar = progress_bar(pct)
                    text = (
                        f"📤 <b>[{_idx}/{_total}] Upload</b>\n"
                        f"<code>{html_escape(_name)}</code>\n"
                        f"{bar} <b>{pct:.1f}%</b>\n"
                        f"Sent: {human_size(current)}\n"
                        f"Size: {human_size(total) if total else '—'}\n"
                        f"<i>{sys_stats_line()}</i>"
                    )
                    await progress_edit(status, text)

                try:
                    await send_document_no_reply(
                        client,
                        chat_id,
                        str(final_path),
                        final_path.name,
                        ep_caption,
                        thumb_path,
                        progress=upload_progress,
                    )
                    uploaded_ok = True
                except Exception as e:
                    logger.exception("Upload failed to %s", chat_id)
                    await message.reply(f"⚠️ Upload failed: {e}", parse_mode=enums.ParseMode.HTML)

            if not KEEP_FILES and uploaded_ok:
                try:
                    final_path.unlink(missing_ok=True)
                except Exception:
                    pass

    await progress_edit(
        status,
        f"🎉 Done! <b>{total_eps}</b> episode(s) / <b>{total_series}</b> series.\n"
        "/status or /clear when finished.",
    )


if __name__ == "__main__":
    # Aeon-style: pull UPSTREAM_REPO even when process is `python -u bot.py`
    try:
        import update as _upstream_update

        _upstream_update.main()
    except Exception as e:
        logger.warning("upstream update skipped: %s", e)

    logger.info("Checking dependencies…")
    ensure_dependencies()
    logger.info("Starting HStream-TG with wzgram (MTProto)…")
    logger.info("UPLOAD_CHANNEL=%s  DUMP_CHANNEL=%s", UPLOAD_CHAT, DUMP_CHAT)
    app.run()
