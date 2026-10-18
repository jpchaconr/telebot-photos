"""SQLite persistence: per-chat storage configuration, setup tokens and the upload queue."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .crypto import Crypto

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    provider TEXT NOT NULL,
    credentials_enc BLOB NOT NULL,
    settings_json TEXT NOT NULL,
    configured_by INTEGER NOT NULL,
    configured_by_name TEXT NOT NULL,
    configured_at REAL NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS setup_sessions (
    token TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    chat_title TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    chat_title TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    file_unique_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    file_name TEXT,
    mime_type TEXT,
    file_size INTEGER,
    caption TEXT,
    sender_name TEXT NOT NULL,
    sent_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    remote_ref TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (chat_id, file_unique_id)
);

CREATE INDEX IF NOT EXISTS jobs_pending ON jobs (status, next_attempt_at);
"""

JOB_COLUMNS = (
    "id, chat_id, message_id, file_id, file_unique_id, kind, file_name, mime_type, file_size,"
    " caption, sender_name, sent_at, attempts"
)


@dataclass
class ChatConfig:
    chat_id: int
    title: str
    provider: str
    credentials: dict[str, Any]
    settings: dict[str, Any]
    configured_by: int
    configured_by_name: str
    configured_at: float
    paused: bool
    status: str


@dataclass
class SetupSession:
    chat_id: int
    chat_title: str
    user_id: int


@dataclass
class OAuthState:
    provider: str
    chat_id: int
    chat_title: str
    user_id: int
    user_name: str


@dataclass
class NewJob:
    chat_id: int
    message_id: int
    file_id: str
    file_unique_id: str
    kind: str
    file_name: str | None
    mime_type: str | None
    file_size: int | None
    caption: str | None
    sender_name: str
    sent_at: float


@dataclass
class Job(NewJob):
    id: int
    attempts: int


class Database:
    """Thin async wrapper around a single SQLite connection.

    Every public method holds ``_lock`` so that statements issued by concurrent
    coroutines never interleave inside a transaction.
    """

    def __init__(self, path: Path | str, crypto: Crypto) -> None:
        self._path = str(path)
        self._crypto = crypto
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    # --- chats -----------------------------------------------------------------

    def _row_to_chat(self, row: aiosqlite.Row) -> ChatConfig:
        return ChatConfig(
            chat_id=row["chat_id"],
            title=row["title"],
            provider=row["provider"],
            credentials=self._crypto.decrypt_json(row["credentials_enc"]),
            settings=json.loads(row["settings_json"]),
            configured_by=row["configured_by"],
            configured_by_name=row["configured_by_name"],
            configured_at=row["configured_at"],
            paused=bool(row["paused"]),
            status=row["status"],
        )

    async def get_chat(self, chat_id: int) -> ChatConfig | None:
        async with self._lock:
            row = await self._fetchone("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
        return self._row_to_chat(row) if row else None

    async def save_chat(
        self,
        *,
        chat_id: int,
        title: str,
        provider: str,
        credentials: dict[str, Any],
        settings: dict[str, Any],
        configured_by: int,
        configured_by_name: str,
    ) -> None:
        async with self._lock:
            await self.conn.execute(
                """
                INSERT INTO chats (chat_id, title, provider, credentials_enc, settings_json,
                                   configured_by, configured_by_name, configured_at, paused, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'ok')
                ON CONFLICT (chat_id) DO UPDATE SET
                    title = excluded.title,
                    provider = excluded.provider,
                    credentials_enc = excluded.credentials_enc,
                    settings_json = excluded.settings_json,
                    configured_by = excluded.configured_by,
                    configured_by_name = excluded.configured_by_name,
                    configured_at = excluded.configured_at,
                    paused = 0,
                    status = 'ok'
                """,
                (
                    chat_id,
                    title,
                    provider,
                    self._crypto.encrypt_json(credentials),
                    json.dumps(settings),
                    configured_by,
                    configured_by_name,
                    time.time(),
                ),
            )

    async def update_credentials(self, chat_id: int, credentials: dict[str, Any]) -> None:
        async with self._lock:
            await self.conn.execute(
                "UPDATE chats SET credentials_enc = ? WHERE chat_id = ?",
                (self._crypto.encrypt_json(credentials), chat_id),
            )

    async def set_paused(self, chat_id: int, paused: bool) -> bool:
        async with self._lock:
            cursor = await self.conn.execute(
                "UPDATE chats SET paused = ? WHERE chat_id = ?", (int(paused), chat_id)
            )
            return cursor.rowcount > 0

    async def set_status(self, chat_id: int, status: str) -> bool:
        """Set the chat status. Returns True only if the status actually changed."""
        async with self._lock:
            cursor = await self.conn.execute(
                "UPDATE chats SET status = ? WHERE chat_id = ? AND status != ?",
                (status, chat_id, status),
            )
            return cursor.rowcount > 0

    async def delete_chat(self, chat_id: int) -> bool:
        async with self._lock:
            await self.conn.execute("BEGIN")
            try:
                cursor = await self.conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
                await self.conn.execute("DELETE FROM jobs WHERE chat_id = ?", (chat_id,))
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            await self.conn.execute("COMMIT")
            return cursor.rowcount > 0

    async def migrate_chat(self, old_chat_id: int, new_chat_id: int) -> None:
        """A group was upgraded to a supergroup and got a new id."""
        async with self._lock:
            await self.conn.execute("BEGIN")
            try:
                await self.conn.execute(
                    "UPDATE OR IGNORE chats SET chat_id = ? WHERE chat_id = ?",
                    (new_chat_id, old_chat_id),
                )
                await self.conn.execute(
                    "UPDATE OR IGNORE jobs SET chat_id = ? WHERE chat_id = ?",
                    (new_chat_id, old_chat_id),
                )
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            await self.conn.execute("COMMIT")

    # --- one-time tokens -------------------------------------------------------

    async def create_setup_session(
        self, *, chat_id: int, chat_title: str, user_id: int, ttl: float = 900
    ) -> str:
        token = secrets.token_urlsafe(24)
        async with self._lock:
            await self.conn.execute(
                "DELETE FROM setup_sessions WHERE expires_at < ?", (time.time(),)
            )
            await self.conn.execute(
                "INSERT INTO setup_sessions (token, chat_id, chat_title, user_id, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (token, chat_id, chat_title, user_id, time.time() + ttl),
            )
        return token

    async def consume_setup_session(self, token: str, user_id: int) -> SetupSession | None:
        """Return and delete the session if it exists, is not expired and belongs to the user."""
        async with self._lock:
            row = await self._fetchone(
                "DELETE FROM setup_sessions WHERE token = ? AND user_id = ? AND expires_at >= ?"
                " RETURNING chat_id, chat_title, user_id",
                (token, user_id, time.time()),
            )
        return SetupSession(**dict(row)) if row else None

    async def create_oauth_state(
        self,
        *,
        provider: str,
        chat_id: int,
        chat_title: str,
        user_id: int,
        user_name: str,
        ttl: float = 900,
    ) -> str:
        state = secrets.token_urlsafe(32)
        async with self._lock:
            await self.conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (time.time(),))
            await self.conn.execute(
                "INSERT INTO oauth_states"
                " (state, provider, chat_id, chat_title, user_id, user_name, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (state, provider, chat_id, chat_title, user_id, user_name, time.time() + ttl),
            )
        return state

    async def consume_oauth_state(self, state: str, provider: str) -> OAuthState | None:
        async with self._lock:
            row = await self._fetchone(
                "DELETE FROM oauth_states WHERE state = ? AND provider = ? AND expires_at >= ?"
                " RETURNING provider, chat_id, chat_title, user_id, user_name",
                (state, provider, time.time()),
            )
        return OAuthState(**dict(row)) if row else None

    # --- upload queue ----------------------------------------------------------

    async def enqueue_job(self, job: NewJob) -> bool:
        """Queue a file for upload. Returns False if the file was already queued for the chat."""
        now = time.time()
        async with self._lock:
            cursor = await self.conn.execute(
                """
                INSERT OR IGNORE INTO jobs (chat_id, message_id, file_id, file_unique_id, kind,
                    file_name, mime_type, file_size, caption, sender_name, sent_at,
                    next_attempt_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.chat_id,
                    job.message_id,
                    job.file_id,
                    job.file_unique_id,
                    job.kind,
                    job.file_name,
                    job.mime_type,
                    job.file_size,
                    job.caption,
                    job.sender_name,
                    job.sent_at,
                    now,
                    now,
                    now,
                ),
            )
            return cursor.rowcount > 0

    async def claim_next_job(self) -> Job | None:
        """Atomically take the oldest due job of a chat whose storage is usable."""
        now = time.time()
        async with self._lock:
            row = await self._fetchone(
                f"""
                UPDATE jobs SET status = 'in_progress', attempts = attempts + 1, updated_at = ?
                WHERE id = (
                    SELECT jobs.id FROM jobs JOIN chats ON chats.chat_id = jobs.chat_id
                    WHERE jobs.status = 'pending' AND jobs.next_attempt_at <= ?
                      AND chats.status = 'ok'
                    ORDER BY jobs.next_attempt_at, jobs.id
                    LIMIT 1
                )
                RETURNING {JOB_COLUMNS}
                """,
                (now, now),
            )
        return Job(**dict(row)) if row else None

    async def complete_job(self, job_id: int, remote_ref: str) -> None:
        async with self._lock:
            await self.conn.execute(
                "UPDATE jobs SET status = 'done', remote_ref = ?, last_error = NULL, updated_at = ?"
                " WHERE id = ?",
                (remote_ref, time.time(), job_id),
            )

    async def retry_job(
        self, job_id: int, error: str, delay: float, *, count_attempt: bool = True
    ) -> None:
        now = time.time()
        async with self._lock:
            await self.conn.execute(
                "UPDATE jobs SET status = 'pending', last_error = ?, next_attempt_at = ?,"
                " attempts = attempts - ?, updated_at = ? WHERE id = ?",
                (error, now + delay, 0 if count_attempt else 1, now, job_id),
            )

    async def fail_job(self, job_id: int, error: str) -> None:
        async with self._lock:
            await self.conn.execute(
                "UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
                (error, time.time(), job_id),
            )

    async def reset_in_progress(self) -> int:
        """Return jobs interrupted by a restart to the queue."""
        async with self._lock:
            cursor = await self.conn.execute(
                "UPDATE jobs SET status = 'pending', attempts = MAX(attempts - 1, 0)"
                " WHERE status = 'in_progress'"
            )
            return cursor.rowcount

    async def job_stats(self, chat_id: int) -> dict[str, int]:
        async with self._lock:
            rows = await self._fetchall(
                "SELECT status, COUNT(*) AS n FROM jobs WHERE chat_id = ? GROUP BY status",
                (chat_id,),
            )
        stats = {"pending": 0, "in_progress": 0, "done": 0, "failed": 0}
        stats.update({row["status"]: row["n"] for row in rows})
        return stats
