from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telegram import Animation, Chat, Document, Message, PhotoSize, Update, User

from conftest import FakeWorker, configure_chat
from telebot_photos.bot.media import extract_media, media_handler, sender_name

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


@pytest.fixture
def context(db):
    worker = FakeWorker()
    services = SimpleNamespace(db=db, worker=worker)
    return SimpleNamespace(bot_data={"services": services}, worker=worker)


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
    assert job.sent_at == DATE.timestamp()

    await db.set_paused(-100, True)
    other = Update(2, message=message(document=Document("d", "u-doc", file_name="a.pdf")))
    await media_handler(other, context)
    assert (await db.job_stats(-100))["pending"] == 0
