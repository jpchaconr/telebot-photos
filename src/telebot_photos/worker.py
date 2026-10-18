"""Background workers that move queued files from Telegram to each chat's storage."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from telegram import Bot, ReplyParameters
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from .config import Settings
from .db import Database, Job
from .naming import build_remote_name
from .storage.base import (
    PROVIDER_NAMES,
    PermanentStorageError,
    StorageAuthError,
    StorageError,
    UploadRequest,
)
from .storage.registry import BackendRegistry

log = logging.getLogger(__name__)

POLL_INTERVAL = 5.0
# With a local Bot API server, getFile only answers once the whole file is downloaded.
GET_FILE_TIMEOUT = 900.0
AUTH_RETRY_DELAY = 60.0


def backoff(attempts: int) -> float:
    return float(min(30 * 2 ** max(attempts - 1, 0), 3600))


class UploadWorker:
    def __init__(
        self, bot: Bot, db: Database, backends: BackendRegistry, settings: Settings
    ) -> None:
        self._bot = bot
        self._db = db
        self._backends = backends
        self._settings = settings
        self._wakeup = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def notify(self) -> None:
        """Wake the workers up because a new job was queued or a chat became usable."""
        self._wakeup.set()

    async def start(self) -> None:
        restored = await self._db.reset_in_progress()
        if restored:
            log.info("Re-queued %d interrupted uploads", restored)
        self._settings.temp_dir.mkdir(parents=True, exist_ok=True)
        self._tasks = [
            asyncio.create_task(self._run(), name=f"upload-worker-{n}")
            for n in range(self._settings.upload_concurrency)
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _run(self) -> None:
        while True:
            try:
                job = await self._db.claim_next_job()
            except Exception:
                log.exception("Could not read the upload queue")
                job = None
            if job is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wakeup.wait(), POLL_INTERVAL)
                self._wakeup.clear()
                continue
            await self.process(job)

    async def process(self, job: Job) -> None:
        local_path: Path | None = None
        owned_by_bot_api = False
        finished = False
        try:
            chat = await self._db.get_chat(job.chat_id)
            if chat is None:
                await self._db.fail_job(job.id, "El chat ya no está configurado")
                finished = True
                return
            local_path, owned_by_bot_api = await self._fetch(job)
            remote = build_remote_name(
                kind=job.kind,
                sent_at=job.sent_at,
                tz=self._settings.tz,
                sender=job.sender_name,
                message_id=job.message_id,
                file_name=job.file_name,
                mime_type=job.mime_type,
            )
            backend = self._backends.get(chat)
            size = (await asyncio.to_thread(local_path.stat)).st_size
            ref = await backend.upload(
                UploadRequest(
                    local_path=local_path,
                    folder=remote.folder,
                    name=remote.name,
                    mime_type=remote.mime_type,
                    size=size,
                    description=job.caption,
                )
            )
        except StorageAuthError as exc:
            log.warning("Chat %s lost access to its storage: %s", job.chat_id, exc)
            await self._db.retry_job(job.id, str(exc), AUTH_RETRY_DELAY, count_attempt=False)
            self._backends.invalidate(job.chat_id)
            if await self._db.set_status(job.chat_id, "reauth_required"):
                await self._notify_reauth(job, str(exc))
        except RetryAfter as exc:
            delay = exc.retry_after
            seconds = delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay)
            await self._db.retry_job(job.id, "Telegram pidió esperar", seconds, count_attempt=False)
        except (PermanentStorageError, BadRequest, Forbidden) as exc:
            finished = True
            await self._fail(job, _describe(exc))
        except (StorageError, TelegramError, OSError) as exc:
            finished = await self._retry_or_fail(job, _describe(exc))
        except Exception as exc:
            log.exception("Unexpected error uploading job %s", job.id)
            finished = await self._retry_or_fail(job, _describe(exc))
        else:
            finished = True
            await self._db.complete_job(job.id, ref)
            log.info("Uploaded job %s of chat %s as %s", job.id, job.chat_id, ref)
            await self._react(job)
        finally:
            if local_path is not None and (finished or not owned_by_bot_api):
                try:
                    local_path.unlink(missing_ok=True)
                except OSError as exc:
                    # Usually a uid mismatch with the Bot API volume: the disk will fill up.
                    log.warning("Could not delete %s: %s", local_path, exc)

    async def _fetch(self, job: Job) -> tuple[Path, bool]:
        """Return the local path of the file and whether it lives in the Bot API server's dir."""
        tg_file = await self._bot.get_file(job.file_id, read_timeout=GET_FILE_TIMEOUT)
        file_path = tg_file.file_path or ""
        if self._settings.local_bot_api and Path(file_path).is_absolute():
            path = Path(file_path)
            if not await asyncio.to_thread(path.is_file):
                raise StorageError(
                    "El servidor local de la Bot API no comparte el archivo con el bot"
                    f" (no existe {path}); revisa el volumen compartido"
                )
            return path, True
        target = self._settings.temp_dir / f"{job.id}_{job.file_unique_id}"
        await tg_file.download_to_drive(target, read_timeout=GET_FILE_TIMEOUT)
        return target, False

    async def _retry_or_fail(self, job: Job, reason: str) -> bool:
        if job.attempts >= self._settings.max_attempts:
            await self._fail(job, reason)
            return True
        delay = backoff(job.attempts)
        log.warning(
            "Job %s failed (attempt %d), retrying in %.0fs: %s", job.id, job.attempts, delay, reason
        )
        await self._db.retry_job(job.id, reason, delay)
        return False

    async def _fail(self, job: Job, reason: str) -> None:
        log.error("Job %s failed permanently: %s", job.id, reason)
        await self._db.fail_job(job.id, reason)
        await self._send(
            job.chat_id,
            f"❌ No pude guardar este archivo en la nube: {reason}",
            reply_to=job.message_id,
        )

    async def _notify_reauth(self, job: Job, reason: str) -> None:
        chat = await self._db.get_chat(job.chat_id)
        provider = PROVIDER_NAMES.get(chat.provider, chat.provider) if chat else "la nube"
        await self._send(
            job.chat_id,
            f"⚠️ Perdí el acceso a {provider} ({reason}).\n"
            "Un administrador debe usar /configurar para reconectarlo. "
            "Los archivos pendientes se guardarán en cuanto se reconecte.",
        )

    async def _send(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        reply = (
            ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
            if reply_to
            else None
        )
        try:
            await self._bot.send_message(chat_id, text, reply_parameters=reply)
        except TelegramError as exc:
            log.warning("Could not notify chat %s: %s", chat_id, exc)

    async def _react(self, job: Job) -> None:
        if not self._settings.success_reaction:
            return
        try:
            await self._bot.set_message_reaction(
                job.chat_id, job.message_id, reaction=self._settings.success_reaction
            )
        except TelegramError as exc:
            log.debug("Could not react to message %s: %s", job.message_id, exc)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BadRequest) and "too big" in exc.message.lower():
        return "supera el tamaño máximo que el bot puede descargar de Telegram"
    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return message[:300]
