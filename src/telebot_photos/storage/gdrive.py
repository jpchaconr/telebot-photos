"""Google Drive backend (REST API v3, resumable uploads)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import quote

import httpx

from ..oauth.base import TokenManager
from ._http import AuthorizedHttp, error_message, raise_for_status, read_range
from .base import (
    MediaNotFound,
    MediaRequest,
    MediaStream,
    PermanentStorageError,
    StorageAuthError,
    StorageError,
    UploadRequest,
)

API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
# Must be a multiple of 256 KiB.
CHUNK_SIZE = 8 * 1024 * 1024
PROVIDER = "Google Drive"

_RETRYABLE_403 = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _reason(response: httpx.Response) -> str:
    try:
        errors = response.json().get("error", {}).get("errors") or [{}]
    except ValueError:
        return ""
    return str(errors[0].get("reason", ""))


def _check(response: httpx.Response) -> None:
    if response.status_code == 403:
        reason = _reason(response)
        if reason in _RETRYABLE_403:
            raise StorageError(f"{PROVIDER}: {reason}")
        if reason == "storageQuotaExceeded":
            raise PermanentStorageError("La cuenta de Google Drive no tiene espacio disponible")
        raise StorageAuthError(f"{PROVIDER}: sin permiso ({reason or error_message(response)})")
    raise_for_status(response, PROVIDER)


async def find_or_create_folder(api: AuthorizedHttp, name: str, parent_id: str) -> dict[str, str]:
    query = (
        f"name = '{_escape(name)}' and mimeType = '{FOLDER_MIME}'"
        f" and '{_escape(parent_id)}' in parents and trashed = false"
    )
    response = await api.request(
        "GET",
        f"{API}/files",
        params={"q": query, "fields": "files(id,webViewLink)", "spaces": "drive", "pageSize": 1},
    )
    if response.status_code == 404:
        raise StorageAuthError(f"La carpeta de {PROVIDER} ya no existe o el bot perdió el acceso")
    _check(response)
    files = response.json().get("files", [])
    if files:
        return files[0]
    response = await api.request(
        "POST",
        f"{API}/files",
        params={"fields": "id,webViewLink"},
        json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
    )
    if response.status_code == 404:
        raise StorageAuthError(f"La carpeta de {PROVIDER} ya no existe o el bot perdió el acceso")
    _check(response)
    return response.json()


async def create_root_folder(
    http: httpx.AsyncClient, tokens: TokenManager, name: str
) -> dict[str, str]:
    """Create (or reuse) the chat's folder in the root of the user's Drive."""
    folder = await find_or_create_folder(AuthorizedHttp(http, tokens, PROVIDER), name, "root")
    return {"folder_id": folder["id"], "folder_name": name, "folder_url": folder["webViewLink"]}


async def _relay_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    except httpx.HTTPError as exc:
        raise StorageError(f"Error de red con {PROVIDER}: {exc!r}") from exc


class GoogleDriveBackend:
    provider = "gdrive"

    def __init__(self, http: httpx.AsyncClient, tokens: TokenManager, folder_id: str) -> None:
        self._api = AuthorizedHttp(http, tokens, PROVIDER)
        self._root_id = folder_id
        self._subfolders: dict[str, str] = {}
        self._folder_lock = asyncio.Lock()

    async def _subfolder(self, name: str) -> str:
        async with self._folder_lock:
            if name not in self._subfolders:
                folder = await find_or_create_folder(self._api, name, self._root_id)
                self._subfolders[name] = folder["id"]
            return self._subfolders[name]

    async def set_public_link(self, public: bool) -> None:
        """Let anyone with the folder link view it (read only), or take that access away.

        Everything inside the folder, including later uploads, inherits the setting.
        """
        url = f"{API}/files/{self._root_id}/permissions"
        response = await self._api.request(
            "GET", url, params={"fields": "permissions(id,type,role)"}
        )
        if response.status_code == 404:
            raise StorageAuthError(
                f"La carpeta de {PROVIDER} ya no existe o el bot perdió el acceso"
            )
        _check(response)
        read_only = False
        for permission in response.json().get("permissions", []):
            if permission.get("type") != "anyone":
                continue
            if public and permission.get("role") == "reader":
                read_only = True
                continue
            # Link access that could edit or comment goes away too, so it ends up read only.
            _check(await self._api.request("DELETE", f"{url}/{permission['id']}"))
        if public and not read_only:
            response = await self._api.request(
                "POST",
                url,
                params={"fields": "id"},
                json={"type": "anyone", "role": "reader", "allowFileDiscovery": False},
            )
            _check(response)

    async def upload(self, request: UploadRequest) -> str:
        parent = await self._subfolder(request.folder)
        metadata: dict[str, object] = {"name": request.name, "parents": [parent]}
        if request.description:
            metadata["description"] = request.description[:4000]
        response = await self._api.request(
            "POST",
            UPLOAD_API,
            params={"uploadType": "resumable", "fields": "id"},
            headers={
                "X-Upload-Content-Type": request.mime_type,
                "X-Upload-Content-Length": str(request.size),
            },
            json=metadata,
        )
        if response.status_code == 404:
            # The month folder was deleted: forget it so the retry creates it again.
            self._subfolders.pop(request.folder, None)
            raise StorageError(f"La carpeta {request.folder} ya no existe en {PROVIDER}")
        _check(response)
        session_url = response.headers["Location"]

        if request.size == 0:
            response = await self._api.request(
                "PUT", session_url, headers={"Content-Range": "bytes */0"}, content=b""
            )
            _check(response)
            return response.json()["id"]

        offset = 0
        stalled = 0
        while True:
            chunk = await read_range(request.local_path, offset, CHUNK_SIZE)
            if not chunk:
                raise StorageError("El archivo local terminó antes de lo esperado")
            end = offset + len(chunk) - 1
            response = await self._api.request(
                "PUT",
                session_url,
                headers={"Content-Range": f"bytes {offset}-{end}/{request.size}"},
                content=chunk,
                timeout=httpx.Timeout(300, connect=15),
            )
            if response.status_code in (200, 201):
                return response.json()["id"]
            if response.status_code != 308:
                _check(response)
                raise StorageError(f"{PROVIDER}: respuesta inesperada {response.status_code}")
            # 308 Resume Incomplete: "Range: bytes=0-N" says what the server already has.
            received = response.headers.get("Range")
            new_offset = int(received.rsplit("-", 1)[1]) + 1 if received else 0
            stalled = stalled + 1 if new_offset <= offset else 0
            if stalled >= 3:
                raise StorageError(f"{PROVIDER} no está aceptando los datos de la subida")
            offset = new_offset

    async def media(self, request: MediaRequest) -> MediaStream:
        """Drive has no pre-signed links: the server relays the file, with ``Range`` for videos."""
        headers = {"Range": request.range_header} if request.range_header else {}
        response = await self._api.request(
            "GET",
            f"{API}/files/{quote(request.ref, safe='')}",
            params={"alt": "media"},
            headers=headers,
            stream=True,
        )
        if response.status_code not in (200, 206):
            await response.aread()
            await response.aclose()
            if response.status_code == 404:
                raise MediaNotFound(f"El archivo ya no está en {PROVIDER}")
            _check(response)
            raise StorageError(f"{PROVIDER}: respuesta inesperada {response.status_code}")
        return MediaStream(
            status=response.status_code,
            chunks=_relay_chunks(response),
            close=response.aclose,
            headers={
                name: response.headers[name]
                for name in ("Content-Length", "Content-Range")
                if name in response.headers
            },
        )

    async def aclose(self) -> None:
        return None
