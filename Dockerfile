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
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["telebot-photos"]
