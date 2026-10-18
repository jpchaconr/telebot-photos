"""Remote folder and file names for uploaded media."""

from __future__ import annotations

import mimetypes
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

# Characters rejected by at least one of the supported providers (OneDrive is the strictest).
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f#%]')
_WHITESPACE = re.compile(r"\s+")
_REPEATED_UNDERSCORES = re.compile(r"_{2,}")

_MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heif",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
    "video/x-matroska": "mkv",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/ogg": "ogg",
    "audio/x-wav": "wav",
    "audio/wav": "wav",
    "audio/flac": "flac",
    "application/pdf": "pdf",
    "application/zip": "zip",
}

_KIND_DEFAULTS = {
    "photo": ("image/jpeg", "jpg"),
    "video": ("video/mp4", "mp4"),
    "animation": ("video/mp4", "mp4"),
    "video_note": ("video/mp4", "mp4"),
    "voice": ("audio/ogg", "ogg"),
    "audio": ("audio/mpeg", "mp3"),
    "document": ("application/octet-stream", "bin"),
}


@dataclass(frozen=True)
class RemoteName:
    folder: str
    name: str
    mime_type: str

    @property
    def path(self) -> str:
        return f"{self.folder}/{self.name}"


def sanitize(name: str, max_length: int = 80, fallback: str = "archivo") -> str:
    """Make a string safe to use as a single path segment on every provider."""
    name = unicodedata.normalize("NFKC", name)
    name = _FORBIDDEN.sub("_", name)
    name = _REPEATED_UNDERSCORES.sub("_", _WHITESPACE.sub("_", name)).strip(" ._")
    if len(name) > max_length:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10 and stem:
            name = stem[: max_length - len(ext) - 1].rstrip(" ._") + "." + ext
        else:
            name = name[:max_length].rstrip(" ._")
    return name or fallback


def resolve_mime_type(kind: str, mime_type: str | None) -> str:
    return mime_type or _KIND_DEFAULTS.get(kind, _KIND_DEFAULTS["document"])[0]


def extension_for(kind: str, mime_type: str | None) -> str:
    if mime_type:
        if mime_type in _MIME_EXTENSIONS:
            return _MIME_EXTENSIONS[mime_type]
        guessed = mimetypes.guess_extension(mime_type)
        if guessed:
            return guessed.lstrip(".")
    return _KIND_DEFAULTS.get(kind, _KIND_DEFAULTS["document"])[1]


def build_remote_name(
    *,
    kind: str,
    sent_at: float,
    tz: ZoneInfo,
    sender: str,
    message_id: int,
    file_name: str | None,
    mime_type: str | None,
) -> RemoteName:
    """``2026-10/20261018-153000_Ana_1234.jpg`` or ``…_1234_original-name.pdf``."""
    moment = datetime.fromtimestamp(sent_at, tz=UTC).astimezone(tz)
    mime = resolve_mime_type(kind, mime_type)
    prefix = f"{moment:%Y%m%d-%H%M%S}_{sanitize(sender, 32, 'anonimo')}_{message_id}"
    if file_name:
        original = sanitize(PurePosixPath(file_name.replace("\\", "/")).name)
        if "." not in original.strip("."):
            original = f"{original}.{extension_for(kind, mime)}"
        name = f"{prefix}_{original}"
    else:
        name = f"{prefix}.{extension_for(kind, mime)}"
    return RemoteName(folder=f"{moment:%Y-%m}", name=name, mime_type=mime)
