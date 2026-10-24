import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from telebot_photos.oauth.base import TokenManager
from telebot_photos.oauth.providers import google_client
from telebot_photos.storage import gdrive
from telebot_photos.storage.base import (
    PermanentStorageError,
    StorageAuthError,
    UploadRequest,
)

TOKEN_URL = "https://oauth2.googleapis.com/token"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
SESSION_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=X"


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def persisted() -> list[dict]:
    return []


@pytest.fixture
def tokens(settings, http, persisted) -> TokenManager:
    async def save(credentials: dict) -> None:
        persisted.append(credentials)

    credentials = {"access_token": "old", "refresh_token": "refresh", "expires_at": 0}
    return TokenManager(google_client(settings, http), credentials, save)


def test_authorization_url_requests_offline_access(settings):
    url = google_client(settings, httpx.AsyncClient()).authorization_url("STATE")
    assert "access_type=offline" in url and "prompt=consent" in url
    assert "drive.file" in url and "state=STATE" in url
    assert "redirect_uri=https%3A%2F%2Fbot.example.com%2Foauth%2Fgoogle%2Fcallback" in url


@respx.mock
async def test_upload_in_chunks_with_token_refresh(http, tokens, persisted, tmp_path, monkeypatch):
    monkeypatch.setattr(gdrive, "CHUNK_SIZE", 4)
    token = respx.post(TOKEN_URL).respond(json={"access_token": "new", "expires_in": 3600})
    search = respx.get(FILES_URL).respond(json={"files": []})
    create = respx.post(FILES_URL).respond(json={"id": "month-folder", "webViewLink": "x"})
    init = respx.post("https://www.googleapis.com/upload/drive/v3/files").respond(
        200, headers={"Location": SESSION_URL}
    )
    chunks = respx.put(SESSION_URL).mock(
        side_effect=[
            httpx.Response(308, headers={"Range": "bytes=0-3"}),
            httpx.Response(200, json={"id": "file-id"}),
        ]
    )
    source = tmp_path / "a.jpg"
    source.write_bytes(b"abcdefg")
    backend = gdrive.GoogleDriveBackend(http, tokens, "root-folder")

    ref = await backend.upload(
        UploadRequest(source, "2026-10", "a.jpg", "image/jpeg", 7, caption="hola", sender="Ana")
    )

    assert ref == "file-id"
    assert token.call_count == 1
    assert persisted[0]["access_token"] == "new"
    assert persisted[0]["refresh_token"] == "refresh"
    assert "'root-folder' in parents" in search.calls[0].request.url.params["q"]
    assert json.loads(create.calls[0].request.content)["parents"] == ["root-folder"]
    metadata = json.loads(init.calls[0].request.content)
    assert metadata == {
        "name": "a.jpg",
        "parents": ["month-folder"],
        "description": "Enviado por: Ana\nComentario: hola",
    }
    assert init.calls[0].request.headers["X-Upload-Content-Length"] == "7"
    assert init.calls[0].request.headers["Authorization"] == "Bearer new"
    assert chunks.calls[0].request.headers["Content-Range"] == "bytes 0-3/7"
    assert chunks.calls[1].request.headers["Content-Range"] == "bytes 4-6/7"
    assert chunks.calls[1].request.content == b"efg"

    # The month folder is cached for the next upload.
    await_again = respx.put(SESSION_URL).respond(200, json={"id": "file-2"})
    assert await backend.upload(UploadRequest(source, "2026-10", "b.jpg", "image/jpeg", 4)) in {
        "file-2"
    }
    assert search.call_count == 1 and await_again.called


@respx.mock
async def test_rejected_token_is_refreshed_once(http, tokens):
    tokens._credentials.update(access_token="cached", expires_at=time.time() + 3600)
    respx.post(TOKEN_URL).respond(json={"access_token": "fresh", "expires_in": 3600})
    route = respx.get(FILES_URL).mock(
        side_effect=[
            httpx.Response(401, json={"error": {"message": "expired"}}),
            httpx.Response(200, json={"files": [{"id": "f", "webViewLink": "https://d/f"}]}),
        ]
    )
    folder = await gdrive.create_root_folder(http, tokens, "Telegram - Familia")
    assert folder == {
        "folder_id": "f",
        "folder_name": "Telegram - Familia",
        "folder_url": "https://d/f",
    }
    assert route.calls[1].request.headers["Authorization"] == "Bearer fresh"


@respx.mock
async def test_revoked_refresh_token_is_an_auth_error(http, tokens):
    respx.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    with pytest.raises(StorageAuthError):
        await gdrive.create_root_folder(http, tokens, "x")


@respx.mock
async def test_quota_exceeded_is_permanent(http, tokens, tmp_path: Path):
    respx.post(TOKEN_URL).respond(json={"access_token": "new", "expires_in": 3600})
    respx.get(FILES_URL).respond(json={"files": [{"id": "m", "webViewLink": "x"}]})
    respx.post("https://www.googleapis.com/upload/drive/v3/files").respond(
        403, json={"error": {"errors": [{"reason": "storageQuotaExceeded"}], "message": "full"}}
    )
    source = tmp_path / "a.jpg"
    source.write_bytes(b"a")
    backend = gdrive.GoogleDriveBackend(http, tokens, "root")
    with pytest.raises(PermanentStorageError, match="espacio"):
        await backend.upload(UploadRequest(source, "2026-10", "a.jpg", "image/jpeg", 1))


PERMISSIONS_URL = "https://www.googleapis.com/drive/v3/files/root/permissions"


@respx.mock
async def test_public_link_is_read_only(http, tokens):
    respx.post(TOKEN_URL).respond(json={"access_token": "new", "expires_in": 3600})
    respx.get(PERMISSIONS_URL).respond(
        json={
            "permissions": [
                {"id": "owner", "type": "user", "role": "owner"},
                {"id": "anyoneWithLink", "type": "anyone", "role": "writer"},
            ]
        }
    )
    removed = respx.delete(PERMISSIONS_URL + "/anyoneWithLink").respond(204)
    created = respx.post(PERMISSIONS_URL).respond(json={"id": "anyoneWithLink"})

    await gdrive.GoogleDriveBackend(http, tokens, "root").set_public_link(True)

    # Editing through the link is replaced by read only access.
    assert removed.called
    assert json.loads(created.calls[0].request.content) == {
        "type": "anyone",
        "role": "reader",
        "allowFileDiscovery": False,
    }


@respx.mock
async def test_public_link_already_read_only_is_left_alone(http, tokens):
    respx.post(TOKEN_URL).respond(json={"access_token": "new", "expires_in": 3600})
    respx.get(PERMISSIONS_URL).respond(
        json={"permissions": [{"id": "anyoneWithLink", "type": "anyone", "role": "reader"}]}
    )
    created = respx.post(PERMISSIONS_URL).respond(json={"id": "x"})

    await gdrive.GoogleDriveBackend(http, tokens, "root").set_public_link(True)

    assert not created.called


@respx.mock
async def test_private_link_keeps_people_it_was_shared_with(http, tokens):
    respx.post(TOKEN_URL).respond(json={"access_token": "new", "expires_in": 3600})
    respx.get(PERMISSIONS_URL).respond(
        json={
            "permissions": [
                {"id": "owner", "type": "user", "role": "owner"},
                {"id": "luis", "type": "user", "role": "reader"},
                {"id": "anyoneWithLink", "type": "anyone", "role": "reader"},
            ]
        }
    )
    removed = respx.delete(url__startswith=PERMISSIONS_URL + "/").respond(204)
    created = respx.post(PERMISSIONS_URL).respond(json={"id": "x"})

    await gdrive.GoogleDriveBackend(http, tokens, "root").set_public_link(False)

    assert [str(call.request.url) for call in removed.calls] == [
        PERMISSIONS_URL + "/anyoneWithLink"
    ]
    assert not created.called


@respx.mock
async def test_public_link_of_a_deleted_folder(http, tokens):
    respx.post(TOKEN_URL).respond(json={"access_token": "new", "expires_in": 3600})
    respx.get(PERMISSIONS_URL).respond(404, json={"error": {"message": "File not found"}})
    with pytest.raises(StorageAuthError, match="ya no existe"):
        await gdrive.GoogleDriveBackend(http, tokens, "root").set_public_link(True)
