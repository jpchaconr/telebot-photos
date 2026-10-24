from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telegram import Animation, Chat, Document, Message, PhotoSize, Update, User, Video

from conftest import FakeBot, FakeWorker, configure_chat
from telebot_photos.bot.media import (
    extract_media,
    media_handler,
    sender_id,
    sender_label,
    sender_name,
)

GROUP = Chat(id=-100, type=Chat.SUPERGROUP, title="Familia")
ANA = User(id=1, first_name="Ana", last_name="López", is_bot=False)
DATE = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def message(**kwargs) -> Message:
    return Message(message_id=10, date=DATE, chat=GROUP, from_user=ANA, **kwargs)


def photo_sizes():
    return [
        PhotoSize("small", "u-small", 90, 90, file_size=100),
        PhotoSize("big", "u-big", 1280, 1280, file_size=9000),
    ]


def test_photo_takes_biggest_size():
    info = extract_media(message(photo=photo_sizes()))
    assert info.kind == "photo" and info.file_id == "big" and info.file_unique_id == "u-big"


def test_previews_for_the_gallery():
    sizes = [
        PhotoSize("s", "u-s", 90, 60, file_size=100),
        PhotoSize("m", "u-m", 320, 213, file_size=900),
        PhotoSize("x", "u-x", 800, 533, file_size=5000),
    ]
    assert extract_media(message(photo=sizes)).thumb_file_id == "m"
    # Without a 320 px size, the smallest one that is big enough.
    assert extract_media(message(photo=photo_sizes())).thumb_file_id == "big"
    clip = Video("vid", "u-vid", 1920, 1080, 6, thumbnail=PhotoSize("t", "u-t", 320, 180))
    assert extract_media(message(video=clip)).thumb_file_id == "t"
    assert extract_media(message(video=Video("v", "u-v", 10, 10, 1))).thumb_file_id is None


def test_gif_is_an_animation_not_a_document():
    animation = Animation("anim", "u-anim", 10, 10, 1, file_name="x.mp4", mime_type="video/mp4")
    document = Document("anim", "u-anim", file_name="x.mp4", mime_type="video/mp4")
    info = extract_media(message(animation=animation, document=document))
    assert info.kind == "animation" and info.file_name == "x.mp4"


def test_text_message_has_no_media():
    assert extract_media(message(text="hola")) is None


def test_sender_name_prefers_username_then_full_name():
    assert sender_name(message()) == "Ana López"
    named = User(id=2, first_name="Bo", is_bot=False, username="bo_99")
    assert sender_name(Message(1, DATE, GROUP, from_user=named)) == "bo_99"
    assert sender_name(Message(1, DATE, GROUP, sender_chat=GROUP)) == "Familia"


def test_sender_label_has_full_name_and_username():
    assert sender_label(message()) == "Ana López"
    named = User(id=2, first_name="Bo", last_name="Ruiz", is_bot=False, username="bo_99")
    assert sender_label(Message(1, DATE, GROUP, from_user=named)) == "Bo Ruiz (@bo_99)"
    channel = Chat(id=-200, type=Chat.CHANNEL, title="Noticias", username="noticias")
    anonymous = Message(1, DATE, GROUP, from_user=named, sender_chat=channel)
    assert sender_label(anonymous) == "Noticias (@noticias)"
    assert sender_id(anonymous) == -200
    assert sender_id(message()) == 1


@pytest.fixture
def context(db, settings):
    worker = FakeWorker()
    services = SimpleNamespace(db=db, worker=worker, settings=settings)
    return SimpleNamespace(
        bot_data={"services": services}, worker=worker, bot=FakeBot(), services=services
    )


async def test_media_is_queued_only_for_configured_active_chats(db, context):
    update = Update(1, message=message(photo=photo_sizes(), caption="hola"))
    await media_handler(update, context)
    assert (await db.job_stats(-100))["pending"] == 0  # not configured: ignored

    await configure_chat(db)
    await media_handler(update, context)
    await media_handler(update, context)  # duplicate update
    assert (await db.job_stats(-100))["pending"] == 1
    assert context.worker.notified == 1
    job = await db.claim_next_job()
    assert job.caption == "hola" and job.sender_name == "Ana López"
    assert job.sender_id == 1 and job.sender_label == "Ana López"
    assert job.sent_at == DATE.timestamp()

    await db.set_paused(-100, True)
    other = Update(2, message=message(document=Document("d", "u-doc", file_name="a.pdf")))
    await media_handler(other, context)
    assert (await db.job_stats(-100))["pending"] == 0


def video(size: int | None) -> Video:
    return Video("vid", "u-vid", 1920, 1080, 60, mime_type="video/mp4", file_size=size)


async def test_cloud_bot_api_skips_files_over_20_mb_with_a_warning(db, context, settings):
    context.services.settings = settings.model_copy(update={"bot_api_url": None})
    await configure_chat(db)

    await media_handler(Update(1, message=message(video=video(35 * 1024 * 1024 + 1))), context)
    assert (await db.job_stats(-100))["pending"] == 0
    assert context.worker.notified == 0
    [warning] = context.bot.messages
    assert warning["chat_id"] == -100
    assert "36 MB" in warning["text"] and "20 MB" in warning["text"]
    assert warning["reply_parameters"].message_id == 10
    assert warning["disable_notification"] is True

    await media_handler(Update(2, message=message(video=video(20 * 1024 * 1024))), context)
    assert (await db.job_stats(-100))["pending"] == 1
    assert len(context.bot.messages) == 1


async def test_size_is_not_checked_with_local_bot_api_or_unknown_size(db, context, settings):
    await configure_chat(db)
    await media_handler(Update(1, message=message(video=video(1500 * 1024 * 1024))), context)
    assert (await db.job_stats(-100))["pending"] == 1

    context.services.settings = settings.model_copy(update={"bot_api_url": None})
    unknown = Document("doc", "u-unknown", file_name="a.zip")
    await media_handler(Update(2, message=message(document=unknown)), context)
    assert (await db.job_stats(-100))["pending"] == 2
    assert context.bot.messages == []
