"""Minimal OAuth 2.0 authorization-code client with refresh-token handling."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from ..storage.base import StorageAuthError, StorageError


@dataclass
class OAuth2Client:
    name: str
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: list[str]
    http: httpx.AsyncClient
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    # Microsoft requires the scopes again when refreshing; Google ignores them.
    send_scope_on_refresh: bool = False

    def authorization_url(self, state: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            **self.extra_auth_params,
        }
        return f"{self.authorize_url}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        tokens = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )
        if not tokens.get("refresh_token"):
            raise StorageAuthError(f"{self.name} no devolvió un refresh token")
        return tokens

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        if self.send_scope_on_refresh:
            data["scope"] = " ".join(self.scopes)
        return await self._token_request(data)

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        data = {**data, "client_id": self.client_id, "client_secret": self.client_secret}
        try:
            response = await self.http.post(self.token_url, data=data)
        except httpx.HTTPError as exc:
            raise StorageError(f"No se pudo contactar con {self.name}: {exc}") from exc
        if response.status_code >= 500:
            raise StorageError(f"{self.name} respondió {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200:
            error = body.get("error", "error")
            if error in {"invalid_grant", "invalid_client", "unauthorized_client"}:
                raise StorageAuthError(f"{self.name}: {error}")
            raise StorageError(f"{self.name}: {error} {body.get('error_description', '')}")
        tokens: dict[str, Any] = {
            "access_token": body["access_token"],
            "expires_at": time.time() + float(body.get("expires_in", 3600)),
        }
        if body.get("refresh_token"):
            tokens["refresh_token"] = body["refresh_token"]
        return tokens


CredentialsCallback = Callable[[dict[str, Any]], Awaitable[None]]


class TokenManager:
    """Keeps a valid access token, refreshing it (and persisting rotated tokens) on demand."""

    def __init__(
        self,
        client: OAuth2Client,
        credentials: dict[str, Any],
        on_update: CredentialsCallback | None = None,
    ) -> None:
        self._client = client
        self._credentials = dict(credentials)
        self._on_update = on_update
        self._lock = asyncio.Lock()

    @property
    def credentials(self) -> dict[str, Any]:
        return dict(self._credentials)

    async def access_token(self) -> str:
        async with self._lock:
            if self._credentials.get("expires_at", 0) - 60 > time.time():
                return self._credentials["access_token"]
            refreshed = await self._client.refresh(self._credentials["refresh_token"])
            self._credentials.update(refreshed)
            if self._on_update is not None:
                await self._on_update(self.credentials)
            return self._credentials["access_token"]

    def invalidate(self) -> None:
        self._credentials["expires_at"] = 0
