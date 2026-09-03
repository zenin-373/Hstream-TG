FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        aria2 \
        curl \
        ca-certificates \
        unzip \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL=/root/.deno
ENV PATH="$DENO_INSTALL/bin:$PATH"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py extractor.py thumb_utils.py update.py start.sh ./
RUN chmod +x start.sh

RUN mkdir -p downloads user_cookies

ENV PYTHONUNBUFFERED=1
ENV DOWNLOAD_ROOT=/app/downloads
ENV COOKIES_DIR=/app/user_cookies
ENV UPSTREAM_REPO=https://github.com/zenin-373/Hstream-TG.git
ENV UPSTREAM_BRANCH=main

CMD ["./start.sh"]
