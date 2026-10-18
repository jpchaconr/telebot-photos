"""Captures the media posted in configured groups and queues it for upload."""

from __future__ import annotations

from dataclasses import dataclass

from telegram import Message, Update
from telegram.ext import ContextTypes, MessageHandler, filters

from ..db import NewJob
from ..services import get_services

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


def extract_media(message: Message) -> MediaInfo | None:
    if message.photo:
        photo = message.photo[-1]  # the biggest size
        return MediaInfo(
            "photo", photo.file_id, photo.file_unique_id, None, "image/jpeg", photo.file_size
        )
    # GIFs come with both ``animation`` and ``document``: check animation first.
    for kind in ("animation", "video", "video_note", "voice", "audio", "document"):
        media = getattr(message, kind)
        if media is not None:
            return MediaInfo(
                kind,
                media.file_id,
                media.file_unique_id,
                getattr(media, "file_name", None),
                getattr(media, "mime_type", None),
                media.file_size,
            )
    return None


def sender_name(message: Message) -> str:
    if message.sender_chat is not None:
        return message.sender_chat.title or message.sender_chat.username or "anonimo"
    user = message.from_user
    if user is None:
        return "anonimo"
    return user.username or user.full_name


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
            sent_at=message.date.timestamp(),
        )
    )
    if queued:
        services.worker.notify()


def build_handlers() -> list:
    return [MessageHandler(MEDIA_FILTER, media_handler)]
