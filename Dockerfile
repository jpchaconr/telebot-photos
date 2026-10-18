FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Same uid/gid as the telegram-bot-api image, so the bot can read (and delete) the files
# that the local Bot API server downloads into the shared volume.
RUN groupadd --system --gid 101 bot \
 && useradd --system --uid 101 --gid 101 --home-dir /data --no-create-home bot \
 && mkdir -p /data/tmp \
 && chown -R bot:bot /data
USER bot

ENV DATABASE_PATH=/data/bot.db \
    TEMP_DIR=/data/tmp
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('WEB_PORT', '8080'))"

# Set by CI (.github/workflows/deploy.yml); reported by /health and in the startup log.
ARG VERSION=dev
ARG REVISION=unknown
ENV APP_VERSION=${VERSION}
LABEL org.opencontainers.image.source="https://github.com/jpchaconr/telebot-photos" \
      org.opencontainers.image.description="Bot de Telegram que guarda la multimedia de cada grupo en su nube" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

CMD ["telebot-photos"]
