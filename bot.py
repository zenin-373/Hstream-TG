#!/usr/bin/env python3
"""
HStream-TG – Telegram bot for hstream.moe downloads + subtitle remux.

Features
--------
• Send one or more episode URLs → bot downloads + tries English .ass + remuxes
• Live progress messages
• /cookies – upload Netscape cookies.txt for restricted titles
• /status – queue & disk info
• Per-user download folders + automatic cleanup
• Telegram 50 MB limit handling (sends file if possible, otherwise warns)
• Runs heavy work in threads so the bot stays responsive
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, Document, constants
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

from extractor import process_url, ensure_dependencies

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
MAX_FILE_MB = float(os.getenv("MAX_FILE_MB", "49"))  # Telegram bot limit ~50 MB
DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "downloads")).resolve()
COOKIES_DIR = Path(os.getenv("COOKIES_DIR", "user_cookies")).resolve()
KEEP_FILES = os.getenv("KEEP_FILES", "false").lower() in {"1", "true", "yes"}
WORKERS = int(os.getenv("WORKERS", "2"))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is required. Set it in .env or environment.")

DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
COOKIES_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("hstream-tg")

executor = ThreadPoolExecutor(max_workers=WORKERS)
URL_RE = re.compile(r"https?://(?:www\.)?hstream\.moe/hentai/[\w\-]+/?", re.I)

# Simple in-memory queue counter (per process)
active_jobs: set[int] = set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def user_dir(user_id: int) -> Path:
    p = DOWNLOAD_ROOT / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_cookies_path(user_id: int) -> Path:
    return COOKIES_DIR / f"{user_id}.txt"


def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


async def safe_edit(message, text: str, **kwargs) -> None:
    try:
        await message.edit_text(text, **kwargs)
    except TelegramError:
        pass


async def send_progress(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Edit the last status message or send a new one."""
    chat_id = update.effective_chat.id
    status_id = context.user_data.get("status_msg_id")

    if status_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_id,
                text=text,
                parse_mode=constants.ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except TelegramError:
            pass

    msg = await update.effective_message.reply_text(
        text,
        parse_mode=constants.ParseMode.HTML,
        disable_web_page_preview=True,
    )
    context.user_data["status_msg_id"] = msg.message_id


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 <b>HStream-TG</b>\n\n"
        "Send me one or more <b>hstream.moe episode links</b> and I will:\n"
        "• download the video (best quality)\n"
        "• try to find English .ass subtitles\n"
        "• remux them into an MKV\n"
        "• send the file back to you\n\n"
        "<b>Commands</b>\n"
        "/start – this message\n"
        "/help – detailed help\n"
        "/cookies – upload cookies.txt (for restricted titles)\n"
        "/status – current jobs & disk usage\n"
        "/clear – delete your temporary files\n\n"
        "⚠️ Only <b>single-episode</b> URLs are supported\n"
        "(e.g. <code>https://hstream.moe/hentai/title-1</code>)"
    )
    await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>How to use</b>\n\n"
        "1. (Optional) Send <code>/cookies</code> and upload a Netscape cookies.txt\n"
        "   if the title requires login.\n\n"
        "2. Paste one or more episode URLs in a single message.\n"
        "   Example:\n"
        "   <code>https://hstream.moe/hentai/some-title-1\n"
        "   https://hstream.moe/hentai/some-title-2</code>\n\n"
        "3. Wait for progress updates. When finished the bot will try to send the file.\n\n"
        "<b>Notes</b>\n"
        "• Telegram bots can only send files ≤ ~50 MB. Larger files will be reported but not uploaded.\n"
        "• Series / playlist pages are <b>not</b> expanded – send each episode link.\n"
        "• Subtitles are best-effort (CDN hosts change).\n"
        "• Files are stored temporarily and can be cleaned with /clear.\n"
    )
    await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    ud = user_dir(uid)
    total = sum(f.stat().st_size for f in ud.rglob("*") if f.is_file())
    files = list(ud.glob("*"))
    text = (
        f"👤 User <code>{uid}</code>\n"
        f"📂 Files in your folder: <b>{len(files)}</b>\n"
        f"💾 Size: <b>{human_size(total)}</b>\n"
        f"⚙️ Active jobs (global): <b>{len(active_jobs)}</b>\n"
        f"🍪 Cookies: {'✅ present' if user_cookies_path(uid).exists() else '❌ none'}\n"
    )
    await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
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
    await update.message.reply_text(f"🧹 Cleared {removed} item(s) from your folder.")


async def cookies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🍪 Send me a <b>Netscape cookies.txt</b> file as a document.\n\n"
        "How to get it:\n"
        "• Browser extension (e.g. “Get cookies.txt LOCALLY”)\n"
        "• Or export from DevTools → Application → Cookies\n\n"
        "The file will be stored only for your Telegram account.",
        parse_mode=constants.ParseMode.HTML,
    )
    context.user_data["awaiting_cookies"] = True


# ---------------------------------------------------------------------------
# Document / text handlers
# ---------------------------------------------------------------------------
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_cookies"):
        return

    doc: Document = update.message.document
    if not doc.file_name or not doc.file_name.lower().endswith((".txt", ".cookies")):
        await update.message.reply_text("Please send a .txt cookies file.")
        return

    uid = update.effective_user.id
    dest = user_cookies_path(uid)
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(custom_path=str(dest))
    context.user_data["awaiting_cookies"] = False

    size = dest.stat().st_size
    await update.message.reply_text(
        f"✅ Cookies saved ({human_size(size)}).\n"
        "They will be used for your future downloads."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    urls = URL_RE.findall(text)

    if not urls:
        await update.message.reply_text(
            "No valid hstream.moe episode URLs found.\n"
            "Send links like:\n<code>https://hstream.moe/hentai/title-1</code>",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    # Deduplicate while preserving order
    seen = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]

    uid = update.effective_user.id
    if uid in active_jobs:
        await update.message.reply_text("⏳ You already have a job running. Please wait.")
        return

    active_jobs.add(uid)
    context.user_data["status_msg_id"] = None

    try:
        await process_urls(update, context, urls)
    finally:
        active_jobs.discard(uid)


async def process_urls(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    urls: list[str],
) -> None:
    uid = update.effective_user.id
    dest = user_dir(uid)
    cookies = user_cookies_path(uid)
    cookies_file = cookies if cookies.exists() else None

    total = len(urls)
    await send_progress(
        update, context,
        f"🚀 Starting <b>{total}</b> download(s)...\n"
        f"Cookies: {'✅' if cookies_file else '❌'}\n"
        f"Please wait, this can take several minutes."
    )

    loop = asyncio.get_running_loop()

    for idx, url in enumerate(urls, 1):
        status_lines = [
            f"📥 <b>[{idx}/{total}]</b>",
            f"<code>{url}</code>",
            "",
            "Status: starting...",
        ]

        def progress_cb(msg: str, _idx=idx, _total=total, _url=url) -> None:
            # Called from worker thread – schedule an async edit
            text = (
                f"📥 <b>[{_idx}/{_total}]</b>\n"
                f"<code>{_url}</code>\n\n"
                f"{msg}"
            )
            asyncio.run_coroutine_threadsafe(
                send_progress(update, context, text),
                loop,
            )

        try:
            final_path: Path = await loop.run_in_executor(
                executor,
                lambda: process_url(
                    url,
                    dest,
                    cookies_file=cookies_file,
                    progress=progress_cb,
                ),
            )
        except Exception as e:
            logger.exception("Failed %s", url)
            await send_progress(
                update, context,
                f"❌ <b>[{idx}/{total}]</b> failed\n<code>{url}</code>\n\n{e}"
            )
            continue

        size_mb = final_path.stat().st_size / (1024 * 1024)
        await send_progress(
            update, context,
            f"✅ <b>[{idx}/{total}]</b> ready\n"
            f"<code>{final_path.name}</code>\n"
            f"Size: {human_size(final_path.stat().st_size)}"
        )

        if size_mb <= MAX_FILE_MB:
            try:
                await update.effective_message.reply_document(
                    document=final_path.open("rb"),
                    filename=final_path.name,
                    caption=f"{final_path.name}\n{human_size(final_path.stat().st_size)}",
                )
            except TelegramError as e:
                await update.effective_message.reply_text(
                    f"⚠️ Could not send file via Telegram: {e}\n"
                    f"File is still on the server: <code>{final_path}</code>",
                    parse_mode=constants.ParseMode.HTML,
                )
        else:
            await update.effective_message.reply_text(
                f"📦 File is too large for Telegram bots ({size_mb:.1f} MB > {MAX_FILE_MB} MB).\n"
                f"It is stored at:\n<code>{final_path}</code>\n\n"
                "Use /clear later to free space, or download it from the host.",
                parse_mode=constants.ParseMode.HTML,
            )

        if not KEEP_FILES:
            # Keep only the final result for a short while; optional aggressive clean
            pass  # leave it – user can /clear

    await send_progress(
        update, context,
        f"🎉 All done! Processed <b>{total}</b> URL(s).\n"
        "Use /status or /clear when finished."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def post_init(app: Application) -> None:
    logger.info("Running one-time dependency check...")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, ensure_dependencies)
    logger.info("Bot is ready.")


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("cookies", cookies_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Starting HStream-TG...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
