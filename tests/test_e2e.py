"""End to end: the real application against a fake Bot API server and a mocked S3."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
import boto3
import pytest
import respx
from aiohttp import web
from aiohttp.test_utils import TestServer
from moto import mock_aws

from telebot_photos.__main__ import serve
from telebot_photos.storage import gdrive

GROUP = {"id": -100, "type": "supergroup", "title": "Familia"}
ANA = {"id": 1, "is_bot": False, "first_name": "Ana", "username": "ana"}
PRIVATE = {"id": 1, "type": "private", "first_name": "Ana"}


class FakeBotApi:
    def __init__(self, files_dir: Path) -> None:
        self.files_dir = files_dir
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._update_id = 0
        self._message_id = 1000
        self._changed = asyncio.Event()

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self.handle)
        return app

    def push(self, kind: str, payload: dict[str, Any]) -> None:
        self._update_id += 1
        self.updates.put_nowait({"update_id": self._update_id, kind: payload})

    def message(self, chat: dict, text: str | None = None, **extra: Any) -> None:
        self._message_id += 1
        payload = {"message_id": self._message_id, "date": int(time.time()), "chat": chat}
        payload["from"] = ANA
        if text is not None:
            payload["text"] = text
            if text.startswith("/"):
                command = text.split()[0]
                payload["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        payload.update(extra)
        self.push("message", payload)

    async def wait_for(
        self, method: str, predicate: Callable[[dict], bool] = lambda _: True, timeout: float = 10
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            for name, params in self.calls:
                if name == method and predicate(params):
                    return params
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"{method} was not called; calls: {self.calls}")
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), remaining)

    async def handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params = dict(await request.post())
        if method != "getUpdates":
            self.calls.append((method, params))
            self._changed.set()
        result = await self.result(method, params)
        return web.json_response({"ok": True, "result": result})

    async def result(self, method: str, params: dict[str, Any]) -> Any:
        if method == "getMe":
            return {
                "id": 999,
                "is_bot": True,
                "first_name": "Fotos",
                "username": "fotos_bot",
                "can_join_groups": True,
                "can_read_all_group_messages": True,
                "supports_inline_queries": False,
            }
        if method == "getUpdates":
            try:
                return [await asyncio.wait_for(self.updates.get(), 0.2)]
            except TimeoutError:
                return []
        if method in {"sendMessage", "editMessageText"}:
            self._message_id += 1
            chat_id = int(params["chat_id"])
            chat = GROUP if chat_id < 0 else PRIVATE
            return {
                "message_id": self._message_id,
                "date": int(time.time()),
                "chat": chat,
                "text": params.get("text", ""),
            }
        if method == "getChatMember":
            return {"status": "creator", "user": ANA, "is_anonymous": False}
        if method == "getFile":
            path = self.files_dir / params["file_id"]
            path.write_bytes(b"\xff\xd8 fake jpeg")
            return {
                "file_id": params["file_id"],
                "file_unique_id": "u-" + params["file_id"],
                "file_size": path.stat().st_size,
                "file_path": str(path),
            }
        return True


@pytest.fixture
def aws():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="fotos")
        yield


def keyboard(params: dict[str, Any]) -> list[list[dict[str, Any]]]:
    return json.loads(params["reply_markup"])["inline_keyboard"]


@pytest.fixture
async def running(settings, tmp_path: Path, unused_tcp_port):
    """Start the whole application against the fake Bot API; yield the fake."""
    files_dir = tmp_path / "bot-api"
    files_dir.mkdir()
    api = FakeBotApi(files_dir)
    server = TestServer(api.app())
    await server.start_server()
    settings.bot_api_url = str(server.make_url("")).rstrip("/")
    settings.web_host = "127.0.0.1"
    settings.web_port = unused_tcp_port

    stop = asyncio.Event()
    app_task = asyncio.create_task(serve(settings, stop))
    try:
        await api.wait_for("deleteWebhook")
        yield api
    finally:
        stop.set()
        await asyncio.wait_for(app_task, 15)
        await server.close()


async def open_setup_menu(api: FakeBotApi) -> list[str]:
    """An admin runs /configurar in the group and follows the deep link to the private chat."""
    api.message(GROUP, "/configurar")
    reply = await api.wait_for("sendMessage", lambda p: p["chat_id"] == "-100")
    link = keyboard(reply)[0][0]["url"]
    assert link.startswith("https://t.me/fotos_bot?start=setup_")
    api.message(PRIVATE, f"/start {link.split('start=')[1]}")
    menu = await api.wait_for("sendMessage", lambda p: "¿Dónde" in p["text"])
    return [row[0]["callback_data"] for row in keyboard(menu)]


def press(api: FakeBotApi, data: str) -> None:
    api.push(
        "callback_query",
        {
            "id": "cb1",
            "from": ANA,
            "chat_instance": "ci",
            "data": data,
            "message": {"message_id": 5, "date": int(time.time()), "chat": PRIVATE},
        },
    )


def send_photo(api: FakeBotApi) -> None:
    api.message(
        GROUP,
        caption="Cumple",
        photo=[
            {"file_id": "small", "file_unique_id": "us", "width": 90, "height": 90},
            {"file_id": "big", "file_unique_id": "ub", "width": 900, "height": 900},
        ],
    )


async def test_configure_s3_and_save_a_photo(aws, running: FakeBotApi):
    api = running
    options = await open_setup_menu(api)
    assert options == ["setup:gdrive", "setup:onedrive", "setup:s3", "setup:cancel"]

    press(api, "setup:s3")
    await api.wait_for("editMessageText", lambda p: "1/6" in p["text"])
    steps = [("aws", "2/6"), ("us-east-1", "3/6"), ("fotos", "4/6"), ("AKID", "5/6")]
    steps += [("SECRET", "6/6")]
    for answer, next_step in steps:
        api.message(PRIVATE, answer)
        await api.wait_for("sendMessage", lambda p, s=next_step: s in p["text"])
    api.message(PRIVATE, "-")
    announce = await api.wait_for(
        "sendMessage", lambda p: p["chat_id"] == "-100" and "Listo" in p["text"]
    )
    assert "s3://fotos/telegram/Familia" in announce["text"]
    deleted = [p for name, p in api.calls if name == "deleteMessage"]
    assert len(deleted) == 2  # both secrets were removed from the chat

    # A photo posted in the group ends up in the bucket and gets a reaction.
    send_photo(api)
    reaction = await api.wait_for("setMessageReaction")
    assert json.loads(reaction["reaction"]) == [{"type": "emoji", "emoji": "👍"}]
    objects = boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="fotos")
    keys = [obj["Key"] for obj in objects.get("Contents", [])]
    assert len([k for k in keys if k.endswith(".jpg")]) == 1
    key = next(k for k in keys if k.endswith(".jpg"))
    assert key.startswith("telegram/Familia/") and "_ana_" in key
    metadata = boto3.client("s3", region_name="us-east-1").head_object(Bucket="fotos", Key=key)
    assert metadata["Metadata"] == {
        "caption": "Cumple",
        "sender": "Ana%20%28%40ana%29",
        "sender-id": "1",
    }
    assert next(c for c in api.calls if c[0] == "getFile")[1]["file_id"] == "big"
    for _ in range(50):  # the file is removed right after the reaction is sent
        if not (api.files_dir / "big").exists():
            break
        await asyncio.sleep(0.02)
    assert not (api.files_dir / "big").exists()

    api.message(GROUP, "/estado")
    status = await api.wait_for("sendMessage", lambda p: "Guardados" in p["text"])
    assert "Guardados: 1" in status["text"]


@pytest.fixture
def google_api():
    with respx.mock() as router:
        router.route(host="127.0.0.1").pass_through()  # the fake Bot API
        routes = {
            "token": router.post("https://oauth2.googleapis.com/token").respond(
                json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
            ),
            "search": router.get(gdrive.API + "/files").respond(json={"files": []}),
            "folder": router.post(gdrive.API + "/files").respond(
                json={"id": "folder-x", "webViewLink": "https://drive.google.com/x"}
            ),
            "init": router.post(gdrive.UPLOAD_API).respond(
                200, headers={"Location": "https://upload.google.test/session"}
            ),
            "put": router.put("https://upload.google.test/session").respond(
                200, json={"id": "drive-file"}
            ),
        }
        yield routes


async def test_configure_google_drive_and_save_a_photo(google_api, running: FakeBotApi, settings):
    api = running
    await open_setup_menu(api)
    press(api, "setup:gdrive")
    prompt = await api.wait_for(
        "editMessageText", lambda p: "Autorizar" in p.get("reply_markup", "")
    )
    authorize = urlparse(keyboard(prompt)[0][0]["url"])
    query = parse_qs(authorize.query)
    assert authorize.netloc == "accounts.google.com"
    assert query["redirect_uri"] == ["https://bot.example.com/oauth/google/callback"]

    # The admin approves in the browser; Google redirects to our callback.
    callback = f"http://127.0.0.1:{settings.web_port}/oauth/google/callback"
    async with aiohttp.ClientSession() as session:
        params = {"state": query["state"][0], "code": "auth-code"}
        async with session.get(callback, params=params) as response:
            assert response.status == 200, await response.text()
    announce = await api.wait_for(
        "sendMessage", lambda p: p["chat_id"] == "-100" and "Listo" in p["text"]
    )
    assert "Google Drive" in announce["text"] and "Telegram - Familia" in announce["text"]
    assert b"code=auth-code" in google_api["token"].calls[0].request.content

    send_photo(api)
    await api.wait_for("setMessageReaction")
    metadata = json.loads(google_api["init"].calls[0].request.content)
    assert metadata["parents"] == ["folder-x"]
    assert metadata["description"] == "Enviado por: Ana (@ana)\nComentario: Cumple"
    assert google_api["put"].calls[0].request.content == b"\xff\xd8 fake jpeg"

    api.message(GROUP, "/enlace")
    link = await api.wait_for("sendMessage", lambda p: "Carpeta en" in p["text"])
    assert "https://drive.google.com/x" in link["text"]
