import time

import aiosqlite

from conftest import configure_chat, new_job
from telebot_photos.crypto import Crypto
from telebot_photos.db import Database


async def test_enqueue_deduplicates_per_chat(db):
    assert await db.enqueue_job(new_job(unique="a"))
    assert not await db.enqueue_job(new_job(unique="a", message_id=11))
    assert await db.enqueue_job(new_job(chat_id=-200, unique="a"))


async def test_old_databases_get_the_new_job_columns(settings, tmp_path):
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as old:
        await old.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,"
            " message_id INTEGER NOT NULL, file_id TEXT NOT NULL, file_unique_id TEXT NOT NULL,"
            " kind TEXT NOT NULL, file_name TEXT, mime_type TEXT, file_size INTEGER, caption TEXT,"
            " sender_name TEXT NOT NULL, sent_at REAL NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,"
            " next_attempt_at REAL NOT NULL DEFAULT 0, last_error TEXT, remote_ref TEXT,"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (chat_id, file_unique_id))"
        )
        await old.execute(
            "INSERT INTO jobs (chat_id, message_id, file_id, file_unique_id, kind, sender_name,"
            " sent_at, created_at, updated_at)"
            " VALUES (-100, 9, 'f', 'old', 'photo', 'ana', 0, 0, 0)"
        )
        await old.commit()

    database = Database(path, Crypto(settings.encryption_key))
    await database.connect()
    try:
        await configure_chat(database)
        await database.enqueue_job(new_job(unique="new", sender_id=5, sender_label="Bo (@bo)"))
        old_job = await database.claim_next_job()
        new = await database.claim_next_job()
    finally:
        await database.close()
    assert old_job.file_unique_id == "old" and old_job.sender_label is None
    assert new.sender_id == 5 and new.sender_label == "Bo (@bo)"


async def test_claim_only_from_usable_chats(db):
    await db.enqueue_job(new_job(unique="a"))
    assert await db.claim_next_job() is None  # chat not configured yet

    await configure_chat(db)
    await db.set_status(-100, "reauth_required")
    assert await db.claim_next_job() is None

    await configure_chat(db)  # reconfiguring resets the status
    job = await db.claim_next_job()
    assert job is not None and job.file_unique_id == "a" and job.attempts == 1
    assert await db.claim_next_job() is None  # already in progress


async def test_retry_delay_and_attempt_counting(db):
    await configure_chat(db)
    await db.enqueue_job(new_job())
    job = await db.claim_next_job()
    await db.retry_job(job.id, "boom", delay=60)
    assert await db.claim_next_job() is None

    await db.conn.execute("UPDATE jobs SET next_attempt_at = 0")
    job = await db.claim_next_job()
    assert job.attempts == 2
    await db.retry_job(job.id, "auth", delay=0, count_attempt=False)
    job = await db.claim_next_job()
    assert job.attempts == 2


async def test_reset_in_progress_and_stats(db):
    await configure_chat(db)
    for unique in ("a", "b", "c"):
        await db.enqueue_job(new_job(unique=unique))
    first = await db.claim_next_job()
    second = await db.claim_next_job()
    await db.complete_job(first.id, "key")
    await db.fail_job(second.id, "nope")
    await db.claim_next_job()
    assert await db.job_stats(-100) == {"pending": 0, "in_progress": 1, "done": 1, "failed": 1}
    assert await db.reset_in_progress() == 1
    assert (await db.job_stats(-100))["pending"] == 1


async def test_credentials_are_encrypted_at_rest(db):
    await configure_chat(db)
    async with db.conn.execute("SELECT credentials_enc FROM chats") as cursor:
        (raw,) = await cursor.fetchone()
    assert b"SECRET" not in raw
    chat = await db.get_chat(-100)
    assert chat.credentials["secret_access_key"] == "SECRET"
    await db.update_credentials(-100, {"access_key_id": "AK", "secret_access_key": "NEW"})
    assert (await db.get_chat(-100)).credentials["secret_access_key"] == "NEW"


async def test_setup_session_is_single_use_and_bound_to_user(db):
    token = await db.create_setup_session(chat_id=-100, chat_title="Familia", user_id=1)
    assert await db.consume_setup_session(token, user_id=2) is None
    session = await db.consume_setup_session(token, user_id=1)
    assert session.chat_id == -100 and session.chat_title == "Familia"
    assert await db.consume_setup_session(token, user_id=1) is None


async def test_setup_session_expires(db):
    token = await db.create_setup_session(chat_id=-100, chat_title="F", user_id=1, ttl=-1)
    assert await db.consume_setup_session(token, user_id=1) is None


async def test_oauth_state_checks_provider_and_is_single_use(db):
    state = await db.create_oauth_state(
        provider="gdrive", chat_id=-100, chat_title="F", user_id=1, user_name="@ana"
    )
    assert await db.consume_oauth_state(state, "onedrive") is None
    pending = await db.consume_oauth_state(state, "gdrive")
    assert pending.user_name == "@ana"
    assert await db.consume_oauth_state(state, "gdrive") is None


async def test_status_change_reports_only_real_changes(db):
    await configure_chat(db)
    assert await db.set_status(-100, "reauth_required")
    assert not await db.set_status(-100, "reauth_required")


async def test_migrate_and_delete_chat(db):
    await configure_chat(db, chat_id=-1)
    await db.enqueue_job(new_job(chat_id=-1))
    await db.migrate_chat(-1, -1001)
    assert await db.get_chat(-1) is None
    assert (await db.get_chat(-1001)).title == "Familia"
    assert (await db.job_stats(-1001))["pending"] == 1

    assert await db.delete_chat(-1001)
    assert await db.get_chat(-1001) is None
    assert (await db.job_stats(-1001))["pending"] == 0


async def test_pause(db):
    assert not await db.set_paused(-100, True)
    await configure_chat(db)
    assert await db.set_paused(-100, True)
    assert (await db.get_chat(-100)).paused
    started = time.time()
    await configure_chat(db)
    chat = await db.get_chat(-100)
    assert not chat.paused and chat.configured_at >= started
