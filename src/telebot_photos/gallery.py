"""Read-only web gallery of a chat's photos, videos and GIFs, opened with a secret link.

Everything is listed from the upload queue in SQLite; the files themselves are read from the
chat's storage with short-lived links (S3, OneDrive) or relayed by the server (Drive), so the
credentials never reach the browser. Nothing here can upload, change or delete files.
"""

from __future__ import annotations

import html
import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from aiohttp import web
from telegram import Bot

from .db import GALLERY_DOCUMENT_TYPES, ChatConfig, GalleryItem
from .naming import build_remote_name, resolve_mime_type
from .services import Services
from .storage.base import (
    MediaNotFound,
    MediaRedirect,
    MediaRequest,
    MediaStream,
    StorageError,
    content_disposition,
)
from .storage.registry import storage_location
from .thumbs import ThumbnailCache

log = logging.getLogger(__name__)

PAGE_SIZE = 60
STATIC_DIR = Path(__file__).parent / "static"
ROOT = "/g/{token:[A-Za-z0-9_-]+}"

# Types the browser may display from this site. Anything else (HTML, SVG...) could run
# scripts, so it is only offered as a download.
INLINE_TYPES = frozenset(GALLERY_DOCUMENT_TYPES)

SECURITY_HEADERS = {
    # Keeps the secret link out of the requests to S3 or OneDrive.
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
}
DEFAULT_CSP = (
    "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'self';"
    " frame-ancestors 'none'; sandbox"
)

PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="light dark">
<title>{title} · Galería</title>
<link rel="stylesheet" href="/static/gallery.css?v={version}">
<script src="/static/gallery.js?v={version}" defer></script>
</head><body>
<header class="top">
<div class="heading"><h1>{title}</h1><p class="summary" id="summary"></p></div>
<div class="filters">
<label class="select"><span class="sr-only">Persona</span>
<select id="sender"><option value="">Todas las personas</option></select></label>
<label class="select"><span class="sr-only">Mes</span>
<select id="month"><option value="">Todos los meses</option></select></label>
</div>
</header>
<main>
<div class="grid" id="grid"></div>
<div class="status" id="status" hidden><p id="status-text"></p>
<button class="button" id="retry" type="button" hidden>Reintentar</button></div>
<div class="more" id="more" aria-hidden="true"></div>
</main>
<div class="viewer" id="viewer" role="dialog" aria-modal="true" aria-label="Visor" hidden>
<div class="viewer-bar">
<button class="icon" id="close" type="button" aria-label="Cerrar">
<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
<a class="button" id="download" href="#" download>
<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11m-5-5l5 5 5-5M5 20h14"/></svg>
Descargar</a>
</div>
<div class="stage" id="stage"></div>
<button class="nav prev" id="prev" type="button" aria-label="Anterior">
<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg></button>
<button class="nav next" id="next" type="button" aria-label="Siguiente">
<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg></button>
<div class="info"><p class="meta"><strong id="info-sender"></strong>
<span aria-hidden="true">·</span> <time id="info-date"></time></p>
<p class="caption" id="info-caption"></p></div>
</div>
<script type="application/json" id="gallery-data">{data}</script>
</body></html>
"""

NOT_FOUND = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Galería no disponible</title>
<link rel="stylesheet" href="/static/gallery.css">
</head><body><main class="notice"><h1>Galería no disponible</h1>
<p>Este enlace no existe o ya no funciona. Pide el enlace actual en el grupo con /galeria.</p>
</main></body></html>
"""


def display_type(item: GalleryItem) -> str:
    """How the page shows the item: ``image``, ``video`` or ``gif`` (a looping silent video)."""
    if item.kind == "animation":
        return "gif"
    return "image" if resolve_mime_type(item.kind, item.mime_type).startswith("image/") else "video"


def item_json(item: GalleryItem) -> dict:
    kind = display_type(item)
    return {
        "id": item.id,
        "type": kind,
        "sender": item.sender_label or item.sender_name,
        "sender_id": item.sender_id,
        "caption": item.caption,
        "sent_at": item.sent_at,
        # Without a Telegram preview, images use the file itself and videos a placeholder.
        "thumb": item.thumb_file_id is not None or kind == "image",
    }


def page_csp(chat: ChatConfig) -> str:
    media = "'self' https:"
    endpoint = chat.settings.get("endpoint_url") if chat.provider == "s3" else None
    if endpoint and endpoint.startswith("http://"):  # a local S3 for testing
        media += f" http://{urlparse(endpoint).netloc}"
    return (
        f"default-src 'none'; script-src 'self'; style-src 'self'; img-src {media};"
        f" media-src {media}; connect-src 'self'; base-uri 'none'; form-action 'none';"
        " frame-ancestors 'none'"
    )


def parse_cursor(value: str | None) -> tuple[float, int] | None:
    if not value:
        return None
    sent_at, _, job_id = value.partition(":")
    return float(sent_at), int(job_id)


def month_range(value: str, tz: ZoneInfo) -> tuple[float, float]:
    """``2026-10`` -> the timestamps where that month starts and ends in the chat's timezone."""
    year, month = (int(part) for part in value.split("-"))
    if not 1 <= month <= 12:
        raise ValueError(f"Mes no válido: {value}")
    start = datetime(year, month, 1, tzinfo=tz)
    end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=tz)
    return start.timestamp(), end.timestamp()


async def add_security_headers(request: web.Request, response: web.StreamResponse) -> None:
    if request.path.startswith("/g/"):
        response.headers.update(SECURITY_HEADERS)
        response.headers.setdefault("Content-Security-Policy", DEFAULT_CSP)
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.path.startswith("/static/"):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Cache-Control", "public, max-age=3600")


class Gallery:
    def __init__(self, services: Services, bot: Bot) -> None:
        self._services = services
        self._thumbs = ThumbnailCache(bot, services.settings)

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get(ROOT, self.page)
        app.router.add_get(ROOT + "/api/items", self.items)
        app.router.add_get(ROOT + r"/thumb/{job_id:\d+}", self.thumb)
        app.router.add_get(ROOT + r"/file/{job_id:\d+}", self.file)
        app.router.add_static("/static/", STATIC_DIR)
        app.on_response_prepare.append(add_security_headers)

    async def _chat(self, request: web.Request) -> tuple[ChatConfig, str]:
        """The chat the link opens and its storage location, or a 404 for unknown links."""
        db = self._services.db
        chat_id = await db.chat_for_gallery(request.match_info["token"])
        chat = await db.get_chat(chat_id) if chat_id is not None else None
        if chat is None:
            raise web.HTTPNotFound(text=NOT_FOUND, content_type="text/html")
        return chat, storage_location(chat)

    async def _item(self, request: web.Request) -> tuple[ChatConfig, GalleryItem]:
        """An item of the link's own chat: ids of other chats are not found."""
        chat, location = await self._chat(request)
        job_id = int(request.match_info["job_id"])
        item = await self._services.db.gallery_item(chat.chat_id, job_id, location)
        if item is None:
            raise web.HTTPNotFound(text="No encontrado")
        return chat, item

    async def page(self, request: web.Request) -> web.Response:
        chat, location = await self._chat(request)
        db, settings = self._services.db, self._services.settings
        senders = await db.gallery_senders(chat.chat_id, location)
        months = Counter(
            datetime.fromtimestamp(sent_at, settings.tz).strftime("%Y-%m")
            for sent_at in await db.gallery_sent_times(chat.chat_id, location)
        )
        data = {
            "base": f"/g/{request.match_info['token']}",
            "timezone": settings.timezone,
            "total": sum(months.values()),
            "senders": [
                {"id": sender.sender_id, "name": sender.label, "count": sender.count}
                for sender in senders
            ],
            "months": [
                {"value": month, "count": count}
                for month, count in sorted(months.items(), reverse=True)
            ],
        }
        body = PAGE.format(
            title=html.escape(chat.title),
            version=html.escape(settings.app_version),
            # "<" can't close the <script> element when written as <.
            data=json.dumps(data, ensure_ascii=False).replace("<", "\\u003c"),
        )
        return web.Response(
            text=body, content_type="text/html", headers={"Content-Security-Policy": page_csp(chat)}
        )

    async def items(self, request: web.Request) -> web.Response:
        chat, location = await self._chat(request)
        query = request.query
        try:
            before = parse_cursor(query.get("cursor"))
            sender_id = int(query["sender"]) if query.get("sender") else None
            start, end = (
                month_range(query["month"], self._services.settings.tz)
                if query.get("month")
                else (None, None)
            )
        except ValueError as exc:
            raise web.HTTPBadRequest(text="Parámetros no válidos") from exc
        rows = await self._services.db.gallery_items(
            chat.chat_id,
            location,
            before=before,
            sender_id=sender_id,
            start=start,
            end=end,
            limit=PAGE_SIZE + 1,
        )
        page = rows[:PAGE_SIZE]
        next_cursor = f"{page[-1].sent_at}:{page[-1].id}" if len(rows) > PAGE_SIZE else None
        return web.json_response({"items": [item_json(item) for item in page], "next": next_cursor})

    async def thumb(self, request: web.Request) -> web.StreamResponse:
        chat, item = await self._item(request)
        if item.thumb_file_id:
            path = await self._thumbs.get(chat.chat_id, item.id, item.thumb_file_id)
            if path is not None:
                return web.FileResponse(path, headers={"Cache-Control": "private, max-age=604800"})
        if display_type(item) == "image":  # saved before previews were recorded
            return await self._serve(request, chat, item, download=False)
        raise web.HTTPNotFound(text="Sin vista previa")

    async def file(self, request: web.Request) -> web.StreamResponse:
        chat, item = await self._item(request)
        return await self._serve(request, chat, item, download=request.query.get("download") == "1")

    async def _serve(
        self, request: web.Request, chat: ChatConfig, item: GalleryItem, *, download: bool
    ) -> web.StreamResponse:
        mime = resolve_mime_type(item.kind, item.mime_type)
        if mime not in INLINE_TYPES:
            mime, download = "application/octet-stream", True
        name = build_remote_name(
            kind=item.kind,
            sent_at=item.sent_at,
            tz=self._services.settings.tz,
            sender=item.sender_name,
            message_id=item.message_id,
            file_name=item.file_name,
            mime_type=item.mime_type,
        ).name
        media_request = MediaRequest(
            item.remote_ref, name, mime, download, request.headers.get("Range")
        )
        try:
            backend = self._services.backends.get(chat)
            media = await backend.media(media_request)
        except MediaNotFound as exc:
            raise web.HTTPNotFound(text="El archivo ya no está en la nube") from exc
        except StorageError as exc:
            log.warning("Gallery could not read job %s of chat %s: %s", item.id, chat.chat_id, exc)
            raise web.HTTPServiceUnavailable(text="No se pudo leer el archivo de la nube") from exc
        if isinstance(media, MediaRedirect):
            # Cached for less time than the link lasts, so it never points to an expired one.
            return web.Response(
                status=302,
                headers={"Location": media.url, "Cache-Control": "private, max-age=300"},
            )
        return await self._relay(request, media, mime, content_disposition(name, download))

    async def _relay(
        self, request: web.Request, media: MediaStream, mime: str, disposition: str
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=media.status,
            headers={
                "Content-Type": mime,
                "Content-Disposition": disposition,
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, max-age=3600",
                **media.headers,
            },
        )
        try:
            await response.prepare(request)
            async for chunk in media.chunks:
                await response.write(chunk)
            await response.write_eof()
        except ConnectionResetError:
            pass  # the browser stopped reading: the viewer was closed or the video seeked
        except StorageError as exc:  # the provider broke off: nothing left to tell the browser
            log.warning("Relaying a file of the gallery failed: %s", exc)
        finally:
            await media.close()
        return response
