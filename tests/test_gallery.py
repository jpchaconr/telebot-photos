import json
import re
import time
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from aiohttp.test_utils import TestClient, TestServer

from conftest import FakeBot, FakeWorker, configure_chat, new_job
from telebot_photos import gallery
from telebot_photos.bot.admin import (
    GALLERY_OFF,
    NOT_CONFIGURED,
    disconnect_callback,
    gallery_callback,
    gallery_command,
)
from telebot_photos.oauth.providers import build_oauth_clients
from telebot_photos.services import Services
from telebot_photos.storage.base import StorageAuthError
from telebot_photos.storage.registry import BackendRegistry
from telebot_photos.web import create_web_app

MADRID = ZoneInfo("Europe/Madrid")
S3_LOCATION = "s3:aws/fotos/telegram"
OCTOBER = datetime(2026, 10, 18, 12, 0, tzinfo=MADRID).timestamp()
SEPTEMBER = datetime(2026, 9, 30, 23, 30, tzinfo=MADRID).timestamp()  # 21:30 UTC


async def save(db, unique: str, location: str | None = S3_LOCATION, **kw) -> int:
    """Queue and complete a job, as the worker does after a successful upload."""
    kw.setdefault("message_id", len(unique) * 100 + sum(map(ord, unique)))
    kw.setdefault("sent_at", OCTOBER)
    await db.enqueue_job(new_job(unique=unique, **kw))
    job = await db.claim_next_job()
    await db.complete_job(job.id, f"ref-{unique}", location)
    return job.id


# --- database --------------------------------------------------------------------------


async def test_gallery_link_can_be_rotated_migrated_and_deleted(db):
    await configure_chat(db)
    token = await db.create_gallery(-100, 1)
    assert len(token) >= 32
    assert await db.get_gallery_token(-100) == token
    assert await db.chat_for_gallery(token) == -100

    new_token = await db.create_gallery(-100, 2)
    assert new_token != token
    assert await db.chat_for_gallery(token) is None
    assert await db.chat_for_gallery(new_token) == -100

    await db.migrate_chat(-100, -1001)
    assert await db.chat_for_gallery(new_token) == -1001
    await db.delete_chat(-1001)
    assert await db.chat_for_gallery(new_token) is None

    await configure_chat(db, chat_id=-5)
    await db.create_gallery(-5, 1)
    assert await db.delete_gallery(-5)
    assert await db.get_gallery_token(-5) is None


async def test_gallery_only_lists_photos_videos_and_gifs_of_the_current_storage(db):
    await configure_chat(db)
    photo = await save(db, "photo")
    video = await save(db, "video", kind="video", mime_type="video/mp4")
    gif = await save(db, "gif", kind="animation", mime_type="video/mp4")
    png = await save(db, "png", kind="document", mime_type="image/png", file_name="a.png")
    old = await save(db, "old", location=None)  # saved before locations were recorded
    await save(db, "voice", kind="voice", mime_type="audio/ogg")
    await save(db, "pdf", kind="document", mime_type="application/pdf", file_name="a.pdf")
    await save(db, "svg", kind="document", mime_type="image/svg+xml", file_name="a.svg")
    await save(db, "elsewhere", location="s3:aws/otro/")
    await db.enqueue_job(new_job(unique="pending"))

    items = await db.gallery_items(-100, S3_LOCATION)
    assert {item.id for item in items} == {photo, video, gif, png, old}
    assert await db.gallery_item(-100, photo, S3_LOCATION) is not None
    assert await db.gallery_item(-200, photo, S3_LOCATION) is None


async def test_gallery_filters_and_pages(db):
    await configure_chat(db)
    ana = [await save(db, f"a{n}", sender_label="Ana (@ana)") for n in range(3)]
    luis = await save(db, "luis", sender_id=2, sender_label="Luis", sent_at=SEPTEMBER)

    first = await db.gallery_items(-100, S3_LOCATION, limit=2)
    assert [item.id for item in first] == [ana[2], ana[1]]  # newest first
    cursor = (first[-1].sent_at, first[-1].id)
    rest = await db.gallery_items(-100, S3_LOCATION, before=cursor, limit=2)
    assert [item.id for item in rest] == [ana[0], luis]

    only_luis = await db.gallery_items(-100, S3_LOCATION, sender_id=2)
    assert [item.id for item in only_luis] == [luis]
    start, end = gallery.month_range("2026-09", MADRID)
    september = await db.gallery_items(-100, S3_LOCATION, start=start, end=end)
    assert [item.id for item in september] == [luis]

    senders = [(s.sender_id, s.label, s.count) for s in await db.gallery_senders(-100, S3_LOCATION)]
    assert senders == [(1, "Ana (@ana)", 3), (2, "Luis", 1)]
    assert sorted(await db.gallery_sent_times(-100, S3_LOCATION)) == [SEPTEMBER, *[OCTOBER] * 3]


def test_month_range_uses_the_chat_timezone():
    start, end = gallery.month_range("2026-12", MADRID)
    assert start == datetime(2026, 12, 1, tzinfo=MADRID).timestamp()
    assert end == datetime(2027, 1, 1, tzinfo=MADRID).timestamp()
    for bad in ("2026-13", "2026", "2026-1-1", "abc"):
        with pytest.raises(ValueError):
            gallery.month_range(bad, MADRID)


# --- web -------------------------------------------------------------------------------


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def bot(tmp_path) -> FakeBot:
    files = tmp_path / "bot-api"
    files.mkdir()
    return FakeBot(files)


@pytest.fixture
def services(settings, db, http) -> Services:
    oauth = build_oauth_clients(settings, http)
    return Services(settings, db, http, oauth, BackendRegistry(db, http, oauth), FakeWorker())


@pytest.fixture
async def client(services, bot):
    async with TestClient(TestServer(create_web_app(services, bot))) as test_client:
        yield test_client


def assert_secure(response):
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


async def test_unknown_link_is_not_found(client, db):
    response = await client.get("/g/not-a-real-token-at-all")
    assert response.status == 404
    assert "no disponible" in await response.text()
    assert_secure(response)


async def test_page_has_filters_and_strict_headers(client, db):
    await db.save_chat(
        chat_id=-100,
        title="<script>Familia</script>",
        provider="s3",
        credentials={"access_key_id": "AK", "secret_access_key": "SECRET"},
        settings={"bucket": "fotos", "prefix": "telegram"},
        configured_by=1,
        configured_by_name="@ana",
    )
    await save(db, "a", sender_label="Ana </script> (@ana)")
    await save(db, "b", sender_id=2, sender_label="Luis", sent_at=SEPTEMBER)
    token = await db.create_gallery(-100, 1)

    response = await client.get(f"/g/{token}")
    assert response.status == 200
    assert_secure(response)
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert response.headers["Cache-Control"] == "no-store"
    body = await response.text()
    assert "<script>Familia" not in body and "&lt;script&gt;Familia" in body
    raw = re.search(r'id="gallery-data">(.*?)</script>', body, re.S).group(1)
    assert "</script>" not in raw
    data = json.loads(raw)
    assert data["base"] == f"/g/{token}"
    assert data["total"] == 2
    assert data["timezone"] == "Europe/Madrid"
    assert data["months"] == [{"value": "2026-10", "count": 1}, {"value": "2026-09", "count": 1}]
    assert [s["name"] for s in data["senders"]] == ["Ana </script> (@ana)", "Luis"]

    static = await client.get("/static/gallery.js")
    assert static.status == 200
    assert static.headers["X-Content-Type-Options"] == "nosniff"


async def test_local_s3_endpoint_is_allowed_by_the_csp(client, db):
    await configure_chat(db, endpoint_url="http://localhost:9000")
    token = await db.create_gallery(-100, 1)
    csp = (await client.get(f"/g/{token}")).headers["Content-Security-Policy"]
    assert "img-src 'self' https: http://localhost:9000;" in csp


async def test_items_api_pages_and_filters(client, db, monkeypatch):
    monkeypatch.setattr(gallery, "PAGE_SIZE", 2)
    await configure_chat(db)
    first = await save(db, "a", caption="Playa", thumb_file_id="thumb-a")
    second = await save(db, "b", kind="video", mime_type="video/mp4")
    third = await save(db, "c", kind="animation", mime_type="video/mp4", sender_id=2)
    token = await db.create_gallery(-100, 1)

    response = await client.get(f"/g/{token}/api/items")
    assert response.status == 200
    assert_secure(response)
    page = await response.json()
    assert [item["id"] for item in page["items"]] == [third, second]
    assert page["items"][0]["type"] == "gif" and page["items"][1]["type"] == "video"
    assert page["items"][1]["thumb"] is False  # a video without a Telegram preview
    rest = await (await client.get(f"/g/{token}/api/items", params={"cursor": page["next"]})).json()
    assert rest["next"] is None
    [item] = rest["items"]
    assert item["id"] == first and item["type"] == "image" and item["thumb"] is True
    assert item["caption"] == "Playa" and item["sender"] == "Ana (@ana)"

    luis = await (await client.get(f"/g/{token}/api/items", params={"sender": "2"})).json()
    assert [item["id"] for item in luis["items"]] == [third]
    september = await client.get(f"/g/{token}/api/items", params={"month": "2026-09"})
    assert (await september.json())["items"] == []
    for bad in ({"month": "2026-13"}, {"sender": "x"}, {"cursor": "nope"}):
        assert (await client.get(f"/g/{token}/api/items", params=bad)).status == 400


async def test_s3_files_redirect_to_short_lived_links(client, db):
    await configure_chat(db)
    photo = await save(db, "photo")
    pdf = await save(db, "pdf", kind="document", mime_type="application/pdf", file_name="a.pdf")
    await configure_chat(db, chat_id=-200)
    other = await save(db, "other", chat_id=-200)
    token = await db.create_gallery(-100, 1)

    response = await client.get(f"/g/{token}/file/{photo}", allow_redirects=False)
    assert response.status == 302
    assert_secure(response)
    location = urlparse(response.headers["Location"])
    query = parse_qs(location.query)
    assert location.path.endswith("/ref-photo")
    assert "X-Amz-Signature" in query and query["X-Amz-Expires"] == ["600"]
    assert query["response-content-type"] == ["image/jpeg"]
    assert query["response-content-disposition"][0].startswith("inline;")
    assert "SECRET" not in response.headers["Location"]

    download = await client.get(f"/g/{token}/file/{photo}?download=1", allow_redirects=False)
    disposition = parse_qs(urlparse(download.headers["Location"]).query)
    assert disposition["response-content-disposition"][0].startswith("attachment;")

    # Another chat's file, or one the gallery doesn't show, is not reachable with this link.
    assert (await client.get(f"/g/{token}/file/{other}", allow_redirects=False)).status == 404
    assert (await client.get(f"/g/{token}/file/{pdf}", allow_redirects=False)).status == 404


async def configure_oauth_chat(db, provider: str, settings: dict) -> None:
    await db.save_chat(
        chat_id=-100,
        title="Familia",
        provider=provider,
        credentials={"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600},
        settings=settings,
        configured_by=1,
        configured_by_name="@ana",
    )


@respx.mock
async def test_drive_files_are_relayed_with_range(client, db):
    await configure_oauth_chat(db, "gdrive", {"folder_id": "folder"})
    job = await save(db, "clip", location="gdrive:folder", kind="video", mime_type="video/mp4")
    token = await db.create_gallery(-100, 1)
    drive = respx.get("https://www.googleapis.com/drive/v3/files/ref-clip")
    drive.respond(206, content=b"0123", headers={"Content-Range": "bytes 0-3/10"})

    response = await client.get(f"/g/{token}/file/{job}", headers={"Range": "bytes=0-3"})

    assert response.status == 206
    assert await response.read() == b"0123"
    assert response.headers["Content-Range"] == "bytes 0-3/10"
    assert response.headers["Content-Type"] == "video/mp4"
    assert response.headers["Content-Disposition"].startswith("inline;")
    assert_secure(response)
    sent = drive.calls[0].request
    assert sent.headers["Range"] == "bytes=0-3"
    assert sent.headers["Authorization"] == "Bearer at"
    assert sent.url.params["alt"] == "media"

    drive.respond(404, json={"error": {"message": "File not found"}})
    assert (await client.get(f"/g/{token}/file/{job}")).status == 404


@respx.mock
async def test_onedrive_files_redirect_to_its_download_links(client, db):
    await configure_oauth_chat(db, "onedrive", {"root_path": "Telegram/Familia"})
    job = await save(db, "photo", location="onedrive:Telegram/Familia")
    token = await db.create_gallery(-100, 1)
    graph = respx.get("https://graph.microsoft.com/v1.0/me/drive/items/ref-photo/content")
    graph.respond(302, headers={"Location": "https://public.example/download?x=1"})

    response = await client.get(f"/g/{token}/file/{job}", allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "https://public.example/download?x=1"
    assert graph.calls[0].request.headers["Authorization"] == "Bearer at"

    graph.respond(404, json={"error": {"code": "itemNotFound"}})
    assert (await client.get(f"/g/{token}/file/{job}", allow_redirects=False)).status == 404
    graph.respond(503, json={"error": {"code": "serviceNotAvailable"}})
    assert (await client.get(f"/g/{token}/file/{job}", allow_redirects=False)).status == 503


async def test_previews_are_fetched_once_and_cached(client, db, bot, settings):
    await configure_chat(db)
    photo = await save(db, "photo", thumb_file_id="thumb-photo")
    (bot.files_dir / "thumb-photo").write_bytes(b"jpeg")
    legacy_photo = await save(db, "legacy")
    legacy_video = await save(db, "video", kind="video", mime_type="video/mp4")
    token = await db.create_gallery(-100, 1)

    response = await client.get(f"/g/{token}/thumb/{photo}")
    assert response.status == 200
    assert await response.read() == b"jpeg"
    assert response.headers["Content-Type"] == "image/jpeg"
    assert (settings.thumb_dir / "-100" / f"{photo}.jpg").read_bytes() == b"jpeg"
    # The local Bot API server's copy is removed once cached.
    assert not (bot.files_dir / "thumb-photo").exists()

    bot.get_file_error = RuntimeError("Telegram should not be asked again")
    assert (await client.get(f"/g/{token}/thumb/{photo}")).status == 200

    # Saved before previews were recorded: images use the file itself, videos have none.
    fallback = await client.get(f"/g/{token}/thumb/{legacy_photo}", allow_redirects=False)
    assert fallback.status == 302
    assert (await client.get(f"/g/{token}/thumb/{legacy_video}")).status == 404


# --- /galeria --------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, chat_id: int = -100) -> None:
        self.chat_id = chat_id
        self.sender_chat = None
        self.replies: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append({"text": text, **kwargs})


def command(user_id: int = 1, chat_type: str = "supergroup"):
    message = FakeMessage()
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=-100, type=chat_type),
        effective_user=SimpleNamespace(id=user_id),
    )
    return update, message


class FakeQuery:
    def __init__(self, data: str, user_id: int = 1) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=-100))
        self.answers: list[tuple] = []
        self.edits: list[dict] = []

    async def answer(self, text: str | None = None, **kwargs) -> None:
        self.answers.append((text, kwargs))

    async def edit_message_text(self, text: str, **kwargs) -> None:
        self.edits.append({"text": text, **kwargs})


@pytest.fixture
def context(services):
    return SimpleNamespace(bot_data={"services": services}, bot=FakeBot())


async def test_galeria_is_enabled_by_an_admin_and_shared_with_members(context, db):
    update, message = command()
    await gallery_command(update, context)
    assert message.replies[0]["text"] == NOT_CONFIGURED

    await configure_chat(db)
    update, message = command(user_id=2)  # a member
    await gallery_command(update, context)
    assert message.replies[0]["text"] == GALLERY_OFF
    assert await db.get_gallery_token(-100) is None

    update, message = command(user_id=1)  # an admin
    await gallery_command(update, context)
    token = await db.get_gallery_token(-100)
    [reply] = message.replies
    assert f"https://bot.example.com/g/{token}" in reply["text"]
    assert "activada" in reply["text"]
    assert reply["protect_content"] is True
    assert reply["reply_markup"] is not None

    update, message = command(user_id=2)
    await gallery_command(update, context)
    assert f"/g/{token}" in message.replies[0]["text"]
    assert await db.get_gallery_token(-100) == token

    update, message = command(chat_type="private")
    await gallery_command(update, context)
    assert "dentro del grupo" in message.replies[0]["text"]


async def test_galeria_warns_when_s3_keys_cannot_read(context, db, services, monkeypatch):
    await configure_chat(db)
    await save(db, "photo")

    async def denied(self, key):
        raise StorageAuthError("S3: Access Denied")

    monkeypatch.setattr("telebot_photos.storage.s3.S3Backend.check_read", denied)
    update, message = command()
    await gallery_command(update, context)
    assert "s3:GetObject" in message.replies[0]["text"]


async def test_only_admins_rotate_or_disable_the_link(context, db, settings):
    await configure_chat(db)
    token = await db.create_gallery(-100, 1)

    query = FakeQuery("gallery:rotate", user_id=2)
    await gallery_callback(SimpleNamespace(callback_query=query), context)
    assert query.answers[0][1] == {"show_alert": True}
    assert await db.get_gallery_token(-100) == token

    query = FakeQuery("gallery:rotate")
    await gallery_callback(SimpleNamespace(callback_query=query), context)
    new_token = await db.get_gallery_token(-100)
    assert new_token != token
    assert f"/g/{new_token}" in query.edits[0]["text"]
    assert "anterior ya no funciona" in query.edits[0]["text"]

    previews = settings.thumb_dir / "-100"
    previews.mkdir(parents=True)
    (previews / "1.jpg").write_bytes(b"jpeg")
    query = FakeQuery("gallery:off")
    await gallery_callback(SimpleNamespace(callback_query=query), context)
    assert await db.get_gallery_token(-100) is None
    assert not previews.exists()
    assert "desactivada" in query.edits[0]["text"]


async def test_disconnect_removes_the_gallery_and_its_previews(context, db, settings):
    await configure_chat(db)
    await db.create_gallery(-100, 1)
    previews = settings.thumb_dir / "-100"
    previews.mkdir(parents=True)
    (previews / "1.jpg").write_bytes(b"jpeg")

    query = FakeQuery("disconnect:yes")
    await disconnect_callback(SimpleNamespace(callback_query=query), context)
    assert await db.get_gallery_token(-100) is None
    assert not previews.exists()
