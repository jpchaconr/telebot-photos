"""Authenticated HTTP helper shared by the REST-based backends (Drive and OneDrive)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from ..oauth.base import TokenManager
from .base import PermanentStorageError, StorageAuthError, StorageError


class AuthorizedHttp:
    def __init__(self, http: httpx.AsyncClient, tokens: TokenManager, provider: str) -> None:
        self.http = http
        self.tokens = tokens
        self.provider = provider

    async def request(
        self,
        method: str,
        url: str,
        *,
        authorize: bool = True,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a request, refreshing the access token once if it was rejected."""
        for attempt in range(2):
            request_headers = dict(headers or {})
            if authorize:
                request_headers["Authorization"] = f"Bearer {await self.tokens.access_token()}"
            try:
                response = await self.http.request(method, url, headers=request_headers, **kwargs)
            except httpx.HTTPError as exc:
                raise StorageError(f"Error de red con {self.provider}: {exc!r}") from exc
            if response.status_code == 401 and authorize and attempt == 0:
                self.tokens.invalidate()
                continue
            return response
        return response


def error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error", body)
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)[:200]
    return str(error)[:200]


def raise_for_status(response: httpx.Response, provider: str) -> None:
    """Translate an HTTP error into the storage error hierarchy."""
    status = response.status_code
    if status < 400:
        return
    detail = f"{provider} {status}: {error_message(response)}"
    if status == 401:
        raise StorageAuthError(detail)
    if status in (408, 429) or (status >= 500 and status != 507):
        raise StorageError(detail)
    if status == 507:
        raise PermanentStorageError(f"Sin espacio disponible en {provider}")
    raise PermanentStorageError(detail)


def _read_range(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(size)


async def read_range(path: Path, offset: int, size: int) -> bytes:
    """Read part of a file without blocking the event loop."""
    return await asyncio.to_thread(_read_range, path, offset, size)
