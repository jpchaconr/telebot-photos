"""Builds (and caches) the storage backend configured for each chat."""

from __future__ import annotations

import httpx

from ..db import ChatConfig, Database
from ..oauth.base import OAuth2Client, TokenManager
from ..oauth.providers import OAUTH_FOR_PROVIDER
from .base import PermanentStorageError, StorageBackend
from .gdrive import GoogleDriveBackend
from .onedrive import OneDriveBackend
from .s3 import S3Backend


class BackendRegistry:
    def __init__(
        self, db: Database, http: httpx.AsyncClient, oauth_clients: dict[str, OAuth2Client]
    ) -> None:
        self._db = db
        self._http = http
        self._oauth = oauth_clients
        self._cache: dict[int, tuple[float, StorageBackend]] = {}

    def token_manager(self, chat: ChatConfig) -> TokenManager:
        client = self._oauth.get(OAUTH_FOR_PROVIDER[chat.provider])
        if client is None:
            raise PermanentStorageError(
                f"El proveedor {chat.provider} no está habilitado en este servidor"
            )

        async def persist(credentials: dict) -> None:
            await self._db.update_credentials(chat.chat_id, credentials)

        return TokenManager(client, chat.credentials, persist)

    def create(self, chat: ChatConfig) -> StorageBackend:
        if chat.provider == "gdrive":
            return GoogleDriveBackend(
                self._http, self.token_manager(chat), chat.settings["folder_id"]
            )
        if chat.provider == "onedrive":
            return OneDriveBackend(self._http, self.token_manager(chat), chat.settings["root_path"])
        if chat.provider == "s3":
            return S3Backend(chat.credentials, chat.settings)
        raise PermanentStorageError(f"Proveedor desconocido: {chat.provider}")

    def get(self, chat: ChatConfig) -> StorageBackend:
        """Return the cached backend unless the chat was reconfigured since it was built."""
        cached = self._cache.get(chat.chat_id)
        if cached and cached[0] == chat.configured_at:
            return cached[1]
        backend = self.create(chat)
        self._cache[chat.chat_id] = (chat.configured_at, backend)
        return backend

    def invalidate(self, chat_id: int) -> None:
        self._cache.pop(chat_id, None)

    async def aclose(self) -> None:
        for _, backend in self._cache.values():
            await backend.aclose()
        self._cache.clear()
