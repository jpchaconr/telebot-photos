"""S3-compatible backend (AWS S3, Cloudflare R2, Backblaze B2, MinIO...)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from urllib.parse import quote

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from .base import PermanentStorageError, StorageAuthError, StorageError, UploadRequest

PROVIDER = "S3"
# boto3 defaults to 10 threads x 8 MiB per upload; keep memory low on 1 GB servers.
TRANSFER_CONFIG = TransferConfig(max_concurrency=4)
_AUTH_CODES = {
    "AccessDenied",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "InvalidToken",
    "ExpiredToken",
    "Unauthorized",
    "Forbidden",
    "403",
}
_PERMANENT_CODES = {"NoSuchBucket", "InvalidBucketName", "EntityTooLarge", "QuotaExceeded", "404"}


def translate_error(exc: Exception) -> StorageError:
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        message = str(exc.response.get("Error", {}).get("Message", "")) or code
        if code in _AUTH_CODES:
            return StorageAuthError(f"{PROVIDER}: {message}")
        if code in _PERMANENT_CODES:
            return PermanentStorageError(f"{PROVIDER}: {message}")
        return StorageError(f"{PROVIDER}: {message}")
    if isinstance(exc, EndpointConnectionError):
        return StorageError(f"No se pudo conectar con el endpoint S3: {exc}")
    return StorageError(f"{PROVIDER}: {exc}")


def object_key(prefix: str, folder: str, name: str) -> str:
    return "/".join(part.strip("/") for part in (prefix, folder, name) if part.strip("/"))


class S3Backend:
    provider = "s3"

    def __init__(self, credentials: dict[str, Any], settings: dict[str, Any]) -> None:
        self.bucket: str = settings["bucket"]
        self.prefix: str = settings.get("prefix", "")
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.get("endpoint_url") or None,
            region_name=settings.get("region") or None,
            aws_access_key_id=credentials["access_key_id"],
            aws_secret_access_key=credentials["secret_access_key"],
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=15,
                read_timeout=120,
                # Several S3-compatible services reject the checksums boto3 adds by default.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    async def upload(self, request: UploadRequest) -> str:
        key = object_key(self.prefix, request.folder, request.name)
        extra: dict[str, Any] = {"ContentType": request.mime_type}
        if request.description:
            # User metadata travels in HTTP headers, so keep it ASCII and short.
            extra["Metadata"] = {"caption": quote(request.description[:500])}
        try:
            await asyncio.to_thread(
                self._client.upload_file,
                str(request.local_path),
                self.bucket,
                key,
                ExtraArgs=extra,
                Config=TRANSFER_CONFIG,
            )
        except (BotoCoreError, ClientError) as exc:
            raise translate_error(exc) from exc
        except Exception as exc:  # boto3's S3Transfer wraps errors in its own types
            cause = exc.__cause__ or exc.__context__
            if isinstance(cause, BotoCoreError | ClientError):
                raise translate_error(cause) from exc
            raise StorageError(f"{PROVIDER}: {exc}") from exc
        return key

    async def check(self) -> None:
        """Verify the credentials can write and delete in the bucket."""
        key = object_key(self.prefix, "", ".telebot-test")
        try:
            await asyncio.to_thread(
                self._client.put_object, Bucket=self.bucket, Key=key, Body=b"ok"
            )
        except (BotoCoreError, ClientError) as exc:
            raise translate_error(exc) from exc
        # Some keys may write but not delete; a leftover test object is harmless.
        with contextlib.suppress(BotoCoreError, ClientError):
            await asyncio.to_thread(self._client.delete_object, Bucket=self.bucket, Key=key)

    async def aclose(self) -> None:
        await asyncio.to_thread(self._client.close)
