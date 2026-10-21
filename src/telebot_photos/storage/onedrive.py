"""OneDrive backend (Microsoft Graph, upload sessions)."""

from __future__ import annotations

import asyncio
import contextlib
from urllib.parse import quote

import httpx

from ..oauth.base import TokenManager
from ._http import AuthorizedHttp, raise_for_status, read_range
from .base import (
    MediaNotFound,
    MediaRedirect,
    MediaRequest,
    StorageAuthError,
    StorageError,
    UploadRequest,
)

GRAPH = "https://graph.microsoft.com/v1.0"
# Must be a multiple of 320 KiB.
CHUNK_SIZE = 32 * 320 * 1024
PROVIDER = "OneDrive"


def _item_url(path: str) -> str:
    return f"{GRAPH}/me/drive/root:/{quote(path.strip('/'), safe='/')}:"


async def ensure_folder(api: AuthorizedHttp, path: str) -> dict[str, str]:
    """Create every missing segment of ``path`` and return the folder's id and URL."""
    parent = ""
    for segment in [part for part in path.split("/") if part]:
        children = f"{_item_url(parent)}/children" if parent else f"{GRAPH}/me/drive/root/children"
        response = await api.request(
            "POST",
            children,
            json={"name": segment, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        )
        if response.status_code != 409:  # 409 = already exists
            raise_for_status(response, PROVIDER)
        parent = f"{parent}/{segment}" if parent else segment
    response = await api.request("GET", _item_url(path), params={"select": "id,webUrl"})
    if response.status_code == 404:
        raise StorageAuthError(f"La carpeta {path} ya no existe en {PROVIDER}")
    raise_for_status(response, PROVIDER)
    return response.json()


async def create_root_folder(http: httpx.AsyncClient, tokens: TokenManager, path: str) -> dict:
    folder = await ensure_folder(AuthorizedHttp(http, tokens, PROVIDER), path)
    return {"root_path": path, "folder_url": folder["webUrl"]}


class OneDriveBackend:
    provider = "onedrive"

    def __init__(self, http: httpx.AsyncClient, tokens: TokenManager, root_path: str) -> None:
        self._api = AuthorizedHttp(http, tokens, PROVIDER)
        self._root = root_path.strip("/")
        self._ready_folders: set[str] = set()
        self._folder_lock = asyncio.Lock()

    async def _ensure(self, folder: str) -> None:
        async with self._folder_lock:
            if folder not in self._ready_folders:
                await ensure_folder(self._api, f"{self._root}/{folder}")
                self._ready_folders.add(folder)

    async def upload(self, request: UploadRequest) -> str:
        await self._ensure(request.folder)
        path = f"{self._root}/{request.folder}/{request.name}"

        if request.size == 0:
            response = await self._api.request(
                "PUT",
                f"{_item_url(path)}/content",
                params={"@microsoft.graph.conflictBehavior": "rename"},
                content=b"",
            )
            raise_for_status(response, PROVIDER)
            return response.json()["id"]

        item: dict[str, object] = {"@microsoft.graph.conflictBehavior": "rename"}
        if request.description:
            item["description"] = request.description[:1000]
        response = await self._api.request(
            "POST", f"{_item_url(path)}/createUploadSession", json={"item": item}
        )
        if response.status_code == 404:
            self._ready_folders.discard(request.folder)
            raise StorageError(f"La carpeta {request.folder} ya no existe en {PROVIDER}")
        raise_for_status(response, PROVIDER)
        upload_url = response.json()["uploadUrl"]

        try:
            return await self._send_chunks(upload_url, request)
        except Exception:
            # Free the space reserved by the unfinished session (best effort).
            with contextlib.suppress(StorageError):
                await self._api.request("DELETE", upload_url, authorize=False)
            raise

    async def _send_chunks(self, upload_url: str, request: UploadRequest) -> str:
        offset = 0
        stalled = 0
        while offset < request.size:
            chunk = await read_range(request.local_path, offset, CHUNK_SIZE)
            if not chunk:
                raise StorageError("El archivo local terminó antes de lo esperado")
            end = offset + len(chunk) - 1
            # The upload URL is pre-authenticated: sending a bearer token makes it fail.
            response = await self._api.request(
                "PUT",
                upload_url,
                authorize=False,
                headers={"Content-Range": f"bytes {offset}-{end}/{request.size}"},
                content=chunk,
                timeout=httpx.Timeout(300, connect=15),
            )
            if response.status_code in (200, 201):
                return response.json()["id"]
            raise_for_status(response, PROVIDER)
            ranges = response.json().get("nextExpectedRanges") or [f"{end + 1}-"]
            new_offset = int(ranges[0].split("-", 1)[0])
            stalled = stalled + 1 if new_offset <= offset else 0
            if stalled >= 3:
                raise StorageError(f"{PROVIDER} no está aceptando los datos de la subida")
            offset = new_offset
        raise StorageError(f"{PROVIDER} no confirmó la subida")

    async def media(self, request: MediaRequest) -> MediaRedirect:
        """Graph redirects ``/content`` to a pre-authenticated download URL that expires soon."""
        response = await self._api.request(
            "GET",
            f"{GRAPH}/me/drive/items/{quote(request.ref, safe='')}/content",
            follow_redirects=False,
        )
        if response.status_code == 404:
            raise MediaNotFound(f"El archivo ya no está en {PROVIDER}")
        location = response.headers.get("Location")
        if response.is_redirect and location:
            return MediaRedirect(location)
        raise_for_status(response, PROVIDER)
        raise StorageError(f"{PROVIDER} no devolvió un enlace de descarga")

    async def aclose(self) -> None:
        return None
