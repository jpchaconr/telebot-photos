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
    description: str | None = None


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
