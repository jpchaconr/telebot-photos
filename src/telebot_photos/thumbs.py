"""Previews for the web gallery, kept on disk under ``THUMB_DIR/<chat id>/``.

They are the small sizes (~320 px) Telegram already keeps for every photo and video, fetched
the first time someone opens the gallery, so only chats with a gallery in use have any.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from .config import Settings
from .storage.base import StorageError
from .worker import fetch_file

log = logging.getLogger(__name__)


def chat_dir(settings: Settings, chat_id: int) -> Path:
    return settings.thumb_dir / str(chat_id)


def delete_chat(settings: Settings, chat_id: int) -> None:
    shutil.rmtree(chat_dir(settings, chat_id), ignore_errors=True)


def move_chat(settings: Settings, old_chat_id: int, new_chat_id: int) -> None:
    """A group was upgraded to a supergroup and got a new id."""
    old, new = chat_dir(settings, old_chat_id), chat_dir(settings, new_chat_id)
    if not old.is_dir():
        return
    if new.exists():
        shutil.rmtree(old, ignore_errors=True)
    else:
        old.rename(new)


class ThumbnailCache:
    def __init__(self, bot: Bot, settings: Settings, concurrency: int = 4) -> None:
        self._bot = bot
        self._settings = settings
        # A gallery page asks for dozens of previews at once: don't flood the Bot API.
        self._semaphore = asyncio.Semaphore(concurrency)

    async def get(self, chat_id: int, job_id: int, file_id: str) -> Path | None:
        """The preview's path, downloading it first if needed. None if Telegram fails."""
        path = chat_dir(self._settings, chat_id) / f"{job_id}.jpg"
        if await asyncio.to_thread(path.is_file):
            return path
        async with self._semaphore:
            if await asyncio.to_thread(path.is_file):  # fetched while this request waited
                return path
            try:
                await self._download(file_id, path)
            except (TelegramError, StorageError, OSError) as exc:
                log.warning("Could not fetch the preview of job %s: %s", job_id, exc)
                return None
        return path

    async def _download(self, file_id: str, path: Path) -> None:
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        partial = path.with_name(f"{path.name}.{secrets.token_hex(4)}.part")
        source, owned_by_bot_api = await fetch_file(self._bot, self._settings, file_id, partial)
        try:
            if owned_by_bot_api:
                await asyncio.to_thread(shutil.copyfile, source, partial)
            # Atomic: a concurrent request never serves a half-written file.
            await asyncio.to_thread(os.replace, partial, path)
        finally:
            with contextlib.suppress(OSError):
                partial.unlink(missing_ok=True)
            if owned_by_bot_api:
                with contextlib.suppress(OSError):
                    source.unlink(missing_ok=True)
