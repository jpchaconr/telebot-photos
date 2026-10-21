"""Common interface and errors for the storage backends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class StorageError(Exception):
    """An upload failed. By default the failure is considered transient and is retried."""

    retryable = True


class PermanentStorageError(StorageError):
    """Retrying will not help (missing bucket, quota exceeded, file too big...)."""

    retryable = False


class StorageAuthError(StorageError):
    """Credentials were revoked or are no longer valid: the chat must be reconfigured."""

    retryable = False


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


class StorageBackend(Protocol):
    provider: str

    async def upload(self, request: UploadRequest) -> str:
        """Upload the file and return a provider-specific reference (id, key...)."""
        ...

    async def aclose(self) -> None: ...


PROVIDER_NAMES = {
    "gdrive": "Google Drive",
    "onedrive": "OneDrive",
    "s3": "S3",
}
