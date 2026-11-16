"""Common interface and errors for the storage backends."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import quote


class StorageError(Exception):
    """An upload failed. By default the failure is considered transient and is retried."""

    retryable = True


class PermanentStorageError(StorageError):
    """Retrying will not help (missing bucket, quota exceeded, file too big...)."""

    retryable = False


class StorageAuthError(StorageError):
    """Credentials were revoked or are no longer valid: the chat must be reconfigured."""

    retryable = False


class MediaNotFound(PermanentStorageError):
    """The file is no longer in the storage (someone deleted or moved it)."""


@dataclass(frozen=True)
class UploadRequest:
    local_path: Path
    folder: str
    name: str
    mime_type: str
    size: int
    caption: str | None = None
    sender: str | None = None
    sender_id: int | None = None

    @property
    def description(self) -> str | None:
        """Who sent the file and its caption, for the providers' description field."""
        lines = []
        if self.sender:
            lines.append(f"Enviado por: {self.sender}")
        if self.caption:
            lines.append(f"Comentario: {self.caption}")
        return "\n".join(lines) or None


@dataclass(frozen=True)
class MediaRequest:
    """Read a stored file for the web gallery."""

    ref: str  # what ``upload`` returned
    file_name: str
    mime_type: str
    download: bool = False
    range_header: str | None = None


@dataclass(frozen=True)
class MediaRedirect:
    """A short-lived URL the browser can fetch the file from, without any credentials."""

    url: str


@dataclass
class MediaStream:
    """The file's bytes, relayed by the server for providers without pre-signed links."""

    status: int  # 200, or 206 for a ``Range`` request
    chunks: AsyncIterator[bytes]
    close: Callable[[], Awaitable[None]]
    headers: dict[str, str] = field(default_factory=dict)  # Content-Length, Content-Range


def content_disposition(file_name: str, download: bool) -> str:
    """``Content-Disposition`` with an ASCII fallback and the UTF-8 name (RFC 6266)."""
    fallback = file_name.encode("ascii", "replace").decode().replace("?", "_")
    fallback = fallback.replace("\\", "_").replace('"', "_")
    kind = "attachment" if download else "inline"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(file_name, safe='')}"


class StorageBackend(Protocol):
    provider: str

    async def upload(self, request: UploadRequest) -> str:
        """Upload the file and return a provider-specific reference (id, key...)."""
        ...

    async def media(self, request: MediaRequest) -> MediaRedirect | MediaStream:
        """Give read-only access to an uploaded file, for the web gallery."""
        ...

    async def aclose(self) -> None: ...


PROVIDER_NAMES = {
    "gdrive": "Google Drive",
    "onedrive": "OneDrive",
    "s3": "S3",
}
