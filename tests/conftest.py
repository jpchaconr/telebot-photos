from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet

from telebot_photos.config import Settings
from telebot_photos.crypto import Crypto
from telebot_photos.db import Database, NewJob


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token="123:abc",
        public_base_url="https://bot.example.com/",
        encryption_key=Fernet.generate_key().decode(),
        database_path=tmp_path / "bot.db",
        temp_dir=tmp_path / "tmp",
        bot_api_url="http://telegram-bot-api:8081",
        google_client_id="google-id",
        google_client_secret="google-secret",
        ms_client_id="ms-id",
        ms_client_secret="ms-secret",
        timezone="Europe/Madrid",
        max_attempts=3,
    )


@pytest.fixture
async def db(settings: Settings):
    database = Database(settings.database_path, Crypto(settings.encryption_key))
    await database.connect()
    yield database
    await database.close()


async def configure_chat(
    db: Database, chat_id: int = -100, provider: str = "s3", **settings: Any
) -> None:
    await db.save_chat(
        chat_id=chat_id,
        title="Familia",
        provider=provider,
        credentials={"access_key_id": "AK", "secret_access_key": "SECRET"},
        settings={"bucket": "fotos", "prefix": "telegram", **settings},
        configured_by=1,
        configured_by_name="@ana",
    )


def new_job(chat_id: int = -100, message_id: int = 10, unique: str = "u1", **kw: Any) -> NewJob:
    values: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "file_id": f"file-{unique}",
        "file_unique_id": unique,
        "kind": "photo",
        "file_name": None,
        "mime_type": "image/jpeg",
        "file_size": 4,
        "caption": None,
        "sender_name": "ana",
        "sent_at": time.time(),
    }
    values.update(kw)
    return NewJob(**values)


class FakeBot:
    """Records what the code under test asks Telegram to do."""

    def __init__(self, files_dir: Path | None = None) -> None:
        self.files_dir = files_dir
        self.messages: list[dict[str, Any]] = []
        self.reactions: list[tuple[int, int, Any]] = []
        self.admin_ids: set[int] = {1}
        self.get_file_error: Exception | None = None
        self.username = "fotos_bot"

    async def get_file(self, file_id: str, **_: Any) -> Any:
        if self.get_file_error:
            raise self.get_file_error
        assert self.files_dir is not None
        path = self.files_dir / file_id
        if not path.exists():
            path.write_bytes(b"data")

        async def download_to_drive(target: Path, **_: Any) -> Path:
            Path(target).write_bytes(path.read_bytes())
            return Path(target)

        return SimpleNamespace(file_path=str(path), download_to_drive=download_to_drive)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})

    async def set_message_reaction(self, chat_id: int, message_id: int, reaction: Any) -> None:
        self.reactions.append((chat_id, message_id, reaction))

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        status = "administrator" if user_id in self.admin_ids else "member"
        return SimpleNamespace(status=status)


class FakeWorker:
    def __init__(self) -> None:
        self.notified = 0

    def notify(self) -> None:
        self.notified += 1
