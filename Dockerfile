FROM python:3.12-slim-bookworm

# System tools required by yt-dlp / ffmpeg / aria2
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        aria2 \
        curl \
        ca-certificates \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno (some hanime-plugin features may need it)
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL=/root/.deno
ENV PATH="$DENO_INSTALL/bin:$PATH"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py extractor.py ./

# Runtime dirs
RUN mkdir -p downloads user_cookies

ENV PYTHONUNBUFFERED=1
ENV DOWNLOAD_ROOT=/app/downloads
ENV COOKIES_DIR=/app/user_cookies

CMD ["python", "-u", "bot.py"]
