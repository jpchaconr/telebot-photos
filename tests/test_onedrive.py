import json
import time

import httpx
import pytest
import respx

from telebot_photos.oauth.base import TokenManager
from telebot_photos.oauth.providers import microsoft_client
from telebot_photos.storage import onedrive
from telebot_photos.storage.base import PermanentStorageError, UploadRequest

GRAPH = "https://graph.microsoft.com/v1.0/me/drive"
UPLOAD_URL = "https://upload.example.com/session/123"


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def tokens(settings, http) -> TokenManager:
    credentials = {"access_token": "tok", "refresh_token": "r", "expires_at": time.time() + 3600}
    return TokenManager(microsoft_client(settings, http), credentials)


def test_refresh_sends_scopes(settings):
    client = microsoft_client(settings, httpx.AsyncClient())
    assert client.send_scope_on_refresh
    assert "offline_access" in client.authorization_url("s")
    assert client.token_url.startswith("https://login.microsoftonline.com/common/")


@respx.mock
async def test_create_root_folder_creates_missing_segments(http, tokens):
    top = respx.post(f"{GRAPH}/root/children").respond(409, json={"error": {"code": "exists"}})
    nested = respx.post(f"{GRAPH}/root:/Telegram:/children").respond(201, json={"id": "x"})
    respx.get(f"{GRAPH}/root:/Telegram/Mi%20grupo:").respond(
        json={"id": "folder", "webUrl": "https://onedrive.live.com/folder"}
    )
    folder = await onedrive.create_root_folder(http, tokens, "Telegram/Mi grupo")
    assert folder == {
        "root_path": "Telegram/Mi grupo",
        "folder_url": "https://onedrive.live.com/folder",
    }
    assert json.loads(nested.calls[0].request.content)["name"] == "Mi grupo"
    assert top.called


@respx.mock
async def test_upload_session_in_chunks(http, tokens, tmp_path, monkeypatch):
    monkeypatch.setattr(onedrive, "CHUNK_SIZE", 3)
    respx.post(url__regex=r".*/children$").respond(409)
    respx.get(url__regex=r".*/root:/Telegram/2026-10:(\?.*)?$").respond(
        json={"id": "f", "webUrl": "u"}
    )
    session = respx.post(f"{GRAPH}/root:/Telegram/2026-10/a.mp4:/createUploadSession").respond(
        json={"uploadUrl": UPLOAD_URL}
    )
    chunks = respx.put(UPLOAD_URL).mock(
        side_effect=[
            httpx.Response(202, json={"nextExpectedRanges": ["3-"]}),
            httpx.Response(201, json={"id": "item-id"}),
        ]
    )
    source = tmp_path / "a.mp4"
    source.write_bytes(b"12345")
    backend = onedrive.OneDriveBackend(http, tokens, "/Telegram/")

    ref = await backend.upload(
        UploadRequest(source, "2026-10", "a.mp4", "video/mp4", 5, description="vídeo")
    )

    assert ref == "item-id"
    item = json.loads(session.calls[0].request.content)["item"]
    assert item == {"@microsoft.graph.conflictBehavior": "rename", "description": "vídeo"}
    assert chunks.calls[0].request.headers["Content-Range"] == "bytes 0-2/5"
    assert chunks.calls[1].request.headers["Content-Range"] == "bytes 3-4/5"
    assert "Authorization" not in chunks.calls[0].request.headers


@respx.mock
async def test_full_drive_is_permanent_and_session_is_cancelled(http, tokens, tmp_path):
    respx.post(url__regex=r".*/children$").respond(409)
    respx.get(url__regex=r".*/root:/T/2026-10:(\?.*)?$").respond(json={"id": "f", "webUrl": "u"})
    respx.post(url__regex=r".*createUploadSession$").respond(json={"uploadUrl": UPLOAD_URL})
    respx.put(UPLOAD_URL).respond(507, json={"error": {"code": "quotaLimitReached"}})
    cancel = respx.delete(UPLOAD_URL).respond(204)
    source = tmp_path / "a.jpg"
    source.write_bytes(b"1")
    backend = onedrive.OneDriveBackend(http, tokens, "T")
    with pytest.raises(PermanentStorageError, match="espacio"):
        await backend.upload(UploadRequest(source, "2026-10", "a.jpg", "image/jpeg", 1))
    assert cancel.called
