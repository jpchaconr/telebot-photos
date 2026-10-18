import json

import httpx
import pytest
import respx
from aiohttp.test_utils import TestClient, TestServer

from conftest import FakeBot, FakeWorker
from telebot_photos.oauth.providers import build_oauth_clients
from telebot_photos.services import Services
from telebot_photos.storage.registry import BackendRegistry
from telebot_photos.web import create_web_app


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def services(settings, db, http) -> Services:
    oauth = build_oauth_clients(settings, http)
    return Services(settings, db, http, oauth, BackendRegistry(db, http, oauth), FakeWorker())


@pytest.fixture
async def client(services, bot):
    async with TestClient(TestServer(create_web_app(services, bot))) as test_client:
        yield test_client


async def new_state(db, provider="gdrive", user_id=1):
    return await db.create_oauth_state(
        provider=provider, chat_id=-100, chat_title="Familia", user_id=user_id, user_name="@ana"
    )


async def test_health_reports_version(client, settings):
    settings.app_version = "sha-abc1234"
    response = await client.get("/health")
    assert response.status == 200
    assert await response.json() == {"status": "ok", "version": "sha-abc1234"}


async def test_homepage_and_privacy_policy(client, settings):
    home = await client.get("/")
    assert home.status == 200
    body = await home.text()
    assert "/configurar" in body and 'href="/privacy"' in body
    assert "@fotos_bot" in body

    settings.contact_email = "ana@example.com"
    privacy = await client.get("/privacy")
    assert privacy.status == 200
    text = await privacy.text()
    assert "drive.file" in text and "Limited Use" in text
    assert "mailto:ana@example.com" in text


async def test_unknown_or_used_state_is_rejected(client):
    response = await client.get("/oauth/google/callback", params={"state": "x", "code": "c"})
    assert response.status == 400
    assert "caducado" in await response.text()


async def test_unknown_provider(client):
    response = await client.get("/oauth/dropbox/callback")
    assert response.status == 404


@respx.mock
async def test_google_callback_saves_configuration(client, db, bot, services):
    state = await new_state(db)
    respx.post("https://oauth2.googleapis.com/token").respond(
        json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    )
    respx.get("https://www.googleapis.com/drive/v3/files").respond(json={"files": []})
    create = respx.post("https://www.googleapis.com/drive/v3/files").respond(
        json={"id": "folder-1", "webViewLink": "https://drive.google.com/folder-1"}
    )

    response = await client.get("/oauth/google/callback", params={"state": state, "code": "c"})

    assert response.status == 200, await response.text()
    assert "conectado" in await response.text()
    assert json.loads(create.calls[0].request.content)["name"] == "Telegram - Familia"
    chat = await db.get_chat(-100)
    assert chat.provider == "gdrive"
    assert chat.credentials["refresh_token"] == "rt"
    assert chat.settings["folder_id"] == "folder-1"
    assert chat.settings["folder_url"] == "https://drive.google.com/folder-1"
    assert chat.configured_by_name == "@ana"
    assert [m["chat_id"] for m in bot.messages] == [-100, 1]
    assert services.worker.notified == 1

    again = await client.get("/oauth/google/callback", params={"state": state, "code": "c"})
    assert again.status == 400


@respx.mock
async def test_microsoft_callback_creates_onedrive_folder(client, db):
    state = await new_state(db, provider="onedrive")
    respx.post(url__regex=r"https://login\.microsoftonline\.com/.*/token").respond(
        json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    )
    respx.post(url__regex=r".*/children$").respond(201, json={"id": "x"})
    respx.get(url__regex=r".*/root:/Telegram/Familia:(\?.*)?$").respond(
        json={"id": "f", "webUrl": "https://onedrive.live.com/f"}
    )
    response = await client.get("/oauth/microsoft/callback", params={"state": state, "code": "c"})
    assert response.status == 200, await response.text()
    chat = await db.get_chat(-100)
    assert chat.provider == "onedrive"
    assert chat.settings == {
        "root_path": "Telegram/Familia",
        "folder_url": "https://onedrive.live.com/f",
    }


async def test_denied_authorization_notifies_user(client, db, bot):
    state = await new_state(db)
    response = await client.get(
        "/oauth/google/callback", params={"state": state, "error": "access_denied"}
    )
    assert response.status == 400
    assert await db.get_chat(-100) is None
    assert bot.messages[0]["chat_id"] == 1


@respx.mock
async def test_user_no_longer_admin(client, db, bot):
    state = await new_state(db, user_id=99)
    respx.post("https://oauth2.googleapis.com/token").respond(
        json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
    )
    respx.get("https://www.googleapis.com/drive/v3/files").respond(
        json={"files": [{"id": "f", "webViewLink": "u"}]}
    )
    response = await client.get("/oauth/google/callback", params={"state": state, "code": "c"})
    assert response.status == 502
    assert await db.get_chat(-100) is None
    assert "administrador" in bot.messages[0]["text"]
