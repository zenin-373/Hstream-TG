# HStream-TG

Telegram bot for **hstream.moe** – bulk download + English subtitle remux.

Built on top of [Hstream-Extractor](https://github.com/zenin-373/Hstream-Extractor) logic (yt-dlp + hanime-plugin + ffmpeg).

## Features

- Send one or more **single-episode** URLs → bot downloads, tries `.ass` subs, remuxes to MKV
- Live progress messages (edits the same status bubble)
- `/cookies` – upload Netscape cookies.txt for login-walled titles
- Per-user download folders + `/clear`
- `/status` – jobs & disk usage
- Runs heavy work in a thread pool → bot stays responsive
- Telegram file-size limit handling (~50 MB)
- Docker-ready

## Limitations (same as the original extractor)

- **Only individual episode links** work  
  `https://hstream.moe/hentai/title-1` ✅  
  Series / playlist pages ❌
- Subtitle CDNs rotate – sometimes the bot cannot find the `.ass`
- Telegram bots cannot send files larger than ~50 MB (the file stays on the server)

## Quick start (local)

### 1. Requirements

- Python 3.10+
- `ffmpeg` and `aria2c` in PATH (highly recommended)
- Deno (optional, for some hanime-plugin features)

```bash
# Debian / Ubuntu / WSL
sudo apt update && sudo apt install -y ffmpeg aria2

# macOS
brew install ffmpeg aria2
```

### 2. Clone & install

```bash
git clone https://github.com/zenin-373/Hstream-TG.git
cd Hstream-TG
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env and put your BOT_TOKEN
```

Get a token from [@BotFather](https://t.me/BotFather).

### 4. Run

```bash
python bot.py
```

Open Telegram, start a chat with your bot and send `/start`.

## Docker

```bash
# Build
docker build -t hstream-tg .

# Run (mount a volume so downloads survive)
docker run -d \
  --name hstream-tg \
  -e BOT_TOKEN=your_token_here \
  -v $(pwd)/downloads:/app/downloads \
  -v $(pwd)/user_cookies:/app/user_cookies \
  hstream-tg
```

Or with a `.env` file:

```bash
docker run -d --name hstream-tg --env-file .env \
  -v $(pwd)/downloads:/app/downloads \
  -v $(pwd)/user_cookies:/app/user_cookies \
  hstream-tg
```

## Bot commands

| Command     | Description                                      |
|-------------|--------------------------------------------------|
| `/start`    | Welcome + short guide                            |
| `/help`     | Detailed usage                                   |
| `/cookies`  | Tell the bot you are about to upload cookies.txt |
| `/status`   | Your files, size, active jobs, cookies status    |
| `/clear`    | Delete all temporary files for your account      |

Just paste episode URLs in a normal message (one or many).

## Cookies (restricted titles)

1. Log in on hstream.moe in your browser.
2. Export cookies with any “Get cookies.txt” extension (Netscape format).
3. In the bot: `/cookies` → send the `.txt` file as a document.
4. The file is stored only for your Telegram user ID.

Never commit real cookies.

## Environment variables

See `.env.example`:

| Variable        | Default          | Meaning                              |
|-----------------|------------------|--------------------------------------|
| `BOT_TOKEN`     | *required*       | From @BotFather                      |
| `OWNER_ID`      | `0`              | Your Telegram ID (optional)          |
| `MAX_FILE_MB`   | `49`             | Max size to try sending via Telegram |
| `DOWNLOAD_ROOT` | `downloads`      | Where videos are stored              |
| `COOKIES_DIR`   | `user_cookies`   | Per-user cookies                     |
| `KEEP_FILES`    | `false`          | Keep files after sending             |
| `WORKERS`       | `2`              | Concurrent download threads          |

## Architecture

```
bot.py          ← Telegram handlers + progress UI (async)
extractor.py    ← pure download / subtitle / remux logic (sync, thread-safe)
```

Heavy work (`process_url`) runs inside a `ThreadPoolExecutor` so the event loop stays free.

## Credits

- Site extraction: [hanime-plugin](https://github.com/cynthia2006/hanime-plugin) by cynthia2006
- Original CLI tool: [Hstream-Extractor](https://github.com/zenin-373/Hstream-Extractor)

## License

MIT

## Disclaimer

Personal / educational / archival use only. Respect the site’s Terms of Service and copyright laws.
