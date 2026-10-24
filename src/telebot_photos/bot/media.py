"""Captures the media posted in configured groups and queues it for upload."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from telegram import Message, PhotoSize, ReplyParameters, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes, MessageHandler, filters

from ..db import NewJob
from ..services import get_services

log = logging.getLogger(__name__)

# Smallest preview side wanted for the web gallery's grid (Telegram's "m" size is 320 px).
THUMB_SIDE = 320

MEDIA_FILTER = (
    filters.UpdateType.MESSAGE
    & filters.ChatType.GROUPS
    & (
        filters.PHOTO
        | filters.VIDEO
        | filters.ANIMATION
        | filters.Document.ALL
        | filters.AUDIO
        | filters.VOICE
        | filters.VIDEO_NOTE
    )
)


@dataclass(frozen=True)
class MediaInfo:
    kind: str
    file_id: str
    file_unique_id: str
    file_name: str | None
    mime_type: str | None
    file_size: int | None
    thumb_file_id: str | None = None


def preview_size(sizes: tuple[PhotoSize, ...]) -> PhotoSize:
    """The smallest size that still fills a gallery tile, or the biggest one available."""
    big_enough = [size for size in sizes if max(size.width, size.height) >= THUMB_SIDE]
    if big_enough:
        return min(big_enough, key=lambda size: size.width * size.height)
    return max(sizes, key=lambda size: size.width * size.height)


def extract_media(message: Message) -> MediaInfo | None:
    if message.photo:
        photo = message.photo[-1]  # the biggest size
        return MediaInfo(
            "photo",
            photo.file_id,
            photo.file_unique_id,
            None,
            "image/jpeg",
            photo.file_size,
            preview_size(message.photo).file_id,
        )
    # GIFs come with both ``animation`` and ``document``: check animation first.
    for kind in ("animation", "video", "video_note", "voice", "audio", "document"):
        media = getattr(message, kind)
        if media is not None:
            thumbnail = getattr(media, "thumbnail", None)
            return MediaInfo(
                kind,
                media.file_id,
                media.file_unique_id,
                getattr(media, "file_name", None),
                getattr(media, "mime_type", None),
                media.file_size,
                thumbnail.file_id if thumbnail is not None else None,
            )
    return None


def sender_name(message: Message) -> str:
    if message.sender_chat is not None:
        return message.sender_chat.title or message.sender_chat.username or "anonimo"
    user = message.from_user
    if user is None:
        return "anonimo"
    return user.username or user.full_name


def sender_label(message: Message) -> str:
    """Full name and username of the sender, for the file's metadata: ``Ana López (@ana)``."""
    if message.sender_chat is not None:
        name, username = message.sender_chat.title, message.sender_chat.username
    elif message.from_user is not None:
        name, username = message.from_user.full_name, message.from_user.username
    else:
        return "anónimo"
    if name and username:
        return f"{name} (@{username})"
    return name or (f"@{username}" if username else "anónimo")


def sender_id(message: Message) -> int | None:
    sender = message.sender_chat or message.from_user
    return sender.id if sender is not None else None


async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    info = extract_media(message)
    if info is None:
        return
    services = get_services(context)
    chat = await services.db.get_chat(message.chat_id)
    if chat is None or chat.paused:
        return
    limit = services.settings.max_download_size
    if limit is not None and info.file_size is not None and info.file_size > limit:
        await warn_too_big(context, message, info.file_size, limit)
        return
    queued = await services.db.enqueue_job(
        NewJob(
            chat_id=message.chat_id,
            message_id=message.message_id,
            file_id=info.file_id,
            file_unique_id=info.file_unique_id,
            kind=info.kind,
            file_name=info.file_name,
            mime_type=info.mime_type,
            file_size=info.file_size,
            caption=message.caption,
            sender_name=sender_name(message),
            sender_id=sender_id(message),
            sender_label=sender_label(message),
            sent_at=message.date.timestamp(),
            thumb_file_id=info.thumb_file_id,
        )
    )
    if queued:
        services.worker.notify()


async def warn_too_big(
    context: ContextTypes.DEFAULT_TYPE, message: Message, size: int, limit: int
) -> None:
    """Tell the group right away that a file can't be saved, instead of failing later."""
    megabyte = 1024 * 1024
    log.info("Skipping a %d byte file in chat %s: over the download limit", size, message.chat_id)
    try:
        await context.bot.send_message(
            message.chat_id,
            f"⚠️ No he guardado este archivo: pesa {math.ceil(size / megabyte)} MB y solo "
            f"puedo descargar archivos de hasta {limit // megabyte} MB.",
            reply_parameters=ReplyParameters(
                message_id=message.message_id, allow_sending_without_reply=True
            ),
            disable_notification=True,
        )
    except TelegramError as exc:
        log.warning("Could not warn chat %s about a big file: %s", message.chat_id, exc)


def build_handlers() -> list:
    return [MessageHandler(MEDIA_FILTER, media_handler)]
