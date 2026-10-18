from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from telegram.error import BadRequest

from conftest import FakeBot, configure_chat, new_job
from telebot_photos.storage.base import (
    PermanentStorageError,
    StorageAuthError,
    StorageError,
    UploadRequest,
)
from telebot_photos.worker import UploadWorker, backoff


class FakeBackend:
    provider = "fake"

    def __init__(self) -> None:
        self.uploads: list[tuple[UploadRequest, bytes]] = []
        self.errors: list[Exception] = []

    async def upload(self, request: UploadRequest) -> str:
        if self.errors:
            raise self.errors.pop(0)
        self.uploads.append((request, request.local_path.read_bytes()))
        return f"ref/{request.name}"

    async def aclose(self) -> None:
        return None


class FakeRegistry:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.invalidated: list[int] = []

    def get(self, chat):
        return self.backend

    def invalidate(self, chat_id: int) -> None:
        self.invalidated.append(chat_id)


@pytest.fixture
def files_dir(tmp_path: Path) -> Path:
    path = tmp_path / "bot-api"
    path.mkdir()
    return path


@pytest.fixture
def bot(files_dir: Path) -> FakeBot:
    return FakeBot(files_dir)


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def worker(bot, db, backend, settings) -> UploadWorker:
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    return UploadWorker(bot, db, FakeRegistry(backend), settings)


async def claim(db):
    job = await db.claim_next_job()
    assert job is not None
    return job


async def test_successful_upload_reacts_and_cleans_up(worker, db, bot, backend, files_dir):
    await configure_chat(db)
    await db.enqueue_job(new_job(caption="En la playa", sender_name="Ana López"))
    await worker.process(await claim(db))

    request, content = backend.uploads[0]
    assert content == b"data"
    assert request.size == 4
    assert request.mime_type == "image/jpeg"
    assert request.description == "En la playa"
    assert "_Ana_López_10.jpg" in request.name
    assert len(request.folder) == 7  # YYYY-MM
    assert bot.reactions == [(-100, 10, "👍")]
    assert (await db.job_stats(-100))["done"] == 1
    assert not (files_dir / "file-u1").exists()


async def test_transient_error_is_retried_with_backoff(worker, db, bot, backend, files_dir):
    await configure_chat(db)
    await db.enqueue_job(new_job())
    backend.errors = [StorageError("timeout")]
    await worker.process(await claim(db))

    assert (await db.job_stats(-100))["pending"] == 1
    assert await db.claim_next_job() is None  # waiting for the backoff
    assert (files_dir / "file-u1").exists()  # kept for the retry in local mode
    assert bot.messages == []


async def test_gives_up_after_max_attempts(worker, db, bot, backend, settings):
    await configure_chat(db)
    await db.enqueue_job(new_job())
    backend.errors = [StorageError("boom")] * settings.max_attempts
    for _ in range(settings.max_attempts):
        await worker.process(await claim(db))
        await db.conn.execute("UPDATE jobs SET next_attempt_at = 0")

    assert (await db.job_stats(-100))["failed"] == 1
    assert len(bot.messages) == 1
    assert bot.messages[0]["text"].startswith("❌")
    assert bot.messages[0]["reply_parameters"].message_id == 10


async def test_permanent_error_fails_immediately(worker, db, bot, backend):
    await configure_chat(db)
    await db.enqueue_job(new_job())
    backend.errors = [PermanentStorageError("sin espacio")]
    await worker.process(await claim(db))
    assert (await db.job_stats(-100))["failed"] == 1
    assert "sin espacio" in bot.messages[0]["text"]


async def test_auth_error_pauses_chat_and_notifies_once(worker, db, bot, backend):
    await configure_chat(db)
    await db.enqueue_job(new_job(unique="a"))
    await db.enqueue_job(new_job(unique="b", message_id=11))
    backend.errors = [StorageAuthError("token revocado"), StorageAuthError("token revocado")]

    first = await claim(db)
    second = await claim(db)
    await worker.process(first)
    await worker.process(second)

    chat = await db.get_chat(-100)
    assert chat.status == "reauth_required"
    assert len(bot.messages) == 1 and "/configurar" in bot.messages[0]["text"]
    assert (await db.job_stats(-100))["pending"] == 2
    assert await db.claim_next_job() is None  # nothing is taken until reconfigured

    await configure_chat(db)
    await db.conn.execute("UPDATE jobs SET next_attempt_at = 0")
    job = await claim(db)
    assert job.attempts == 1  # auth failures do not use up attempts


async def test_file_too_big_for_cloud_api(worker, db, bot):
    await configure_chat(db)
    await db.enqueue_job(new_job())
    bot.get_file_error = BadRequest("File is too big")
    await worker.process(await claim(db))
    assert (await db.job_stats(-100))["failed"] == 1
    assert "tamaño máximo" in bot.messages[0]["text"]


async def test_cloud_api_downloads_to_temp_and_always_cleans(db, bot, backend, settings, files_dir):
    settings.bot_api_url = None
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    worker = UploadWorker(bot, db, FakeRegistry(backend), settings)
    await configure_chat(db)
    await db.enqueue_job(new_job())
    backend.errors = [StorageError("timeout")]
    await worker.process(await claim(db))
    assert list(settings.temp_dir.iterdir()) == []

    await db.conn.execute("UPDATE jobs SET next_attempt_at = 0")
    await worker.process(await claim(db))
    assert backend.uploads[0][1] == b"data"
    assert list(settings.temp_dir.iterdir()) == []
    assert (files_dir / "file-u1").exists()  # not ours to delete without a local server


async def test_start_requeues_interrupted_jobs_and_processes_them(worker, db, backend):
    await configure_chat(db)
    await db.enqueue_job(new_job())
    await claim(db)  # simulate a crash while the job was in progress
    await worker.start()
    try:
        for _ in range(50):
            if backend.uploads:
                break
            worker.notify()
            await asyncio.sleep(0.02)
    finally:
        await worker.stop()
    assert len(backend.uploads) == 1


def test_backoff_grows_and_is_capped():
    assert backoff(1) == 30
    assert backoff(2) == 60
    assert backoff(20) == 3600
