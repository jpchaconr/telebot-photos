"""Small web server for the OAuth callbacks and health checks."""

from __future__ import annotations

import html
import logging

from aiohttp import web
from telegram import Bot
from telegram.error import TelegramError

from .bot.setup import SetupError, complete_setup, default_folder_name, default_onedrive_path
from .oauth.base import TokenManager
from .oauth.providers import GOOGLE, MICROSOFT
from .services import Services
from .storage import gdrive, onedrive
from .storage.base import PROVIDER_NAMES, StorageError

log = logging.getLogger(__name__)

# OAuth client used in the callback URL -> storage provider.
CALLBACK_PROVIDERS = {GOOGLE: "gdrive", MICROSOFT: "onedrive"}

SERVICES_KEY = web.AppKey("services", Services)
BOT_KEY = web.AppKey("bot", Bot)

PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f4f5f7;color:#1d2125;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}}
main{{background:#fff;border-radius:12px;padding:32px;max-width:420px;
box-shadow:0 2px 12px rgba(0,0,0,.08);text-align:center}}
h1{{font-size:1.4rem;margin:0 0 12px}}p{{line-height:1.5;margin:0}}
@media (prefers-color-scheme:dark){{body{{background:#161a1d;color:#dee4ea}}
main{{background:#22272b}}}}
</style></head>
<body><main><h1>{title}</h1><p>{body}</p></main></body></html>
"""


def page(title: str, body: str, status: int = 200) -> web.Response:
    return web.Response(
        text=PAGE.format(title=html.escape(title), body=html.escape(body)),
        status=status,
        content_type="text/html",
    )


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def index(_: web.Request) -> web.Response:
    return page("Bot de fotos", "Este servidor atiende las autorizaciones del bot de Telegram.")


async def _tell_user(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text)
    except TelegramError as exc:
        log.warning("Could not message user %s: %s", user_id, exc)


async def oauth_callback(request: web.Request) -> web.Response:
    services = request.app[SERVICES_KEY]
    bot = request.app[BOT_KEY]
    client_name = request.match_info["client"]
    provider = CALLBACK_PROVIDERS.get(client_name)
    client = services.oauth.get(client_name)
    if provider is None or client is None:
        return page("No encontrado", "Este proveedor no está habilitado.", status=404)

    pending = await services.db.consume_oauth_state(request.query.get("state", ""), provider)
    if pending is None:
        return page(
            "Enlace caducado",
            "Este enlace de autorización no es válido o ya se usó. "
            "Vuelve a usar /configurar en el grupo.",
            status=400,
        )
    name = PROVIDER_NAMES[provider]
    if "error" in request.query or not request.query.get("code"):
        await _tell_user(bot, pending.user_id, f"No se autorizó el acceso a {name}.")
        return page("Autorización cancelada", "No se conectó ninguna cuenta.", status=400)

    try:
        credentials = await client.exchange_code(request.query["code"])
        tokens = TokenManager(client, credentials)
        if provider == "gdrive":
            settings = await gdrive.create_root_folder(
                services.http, tokens, default_folder_name(pending.chat_title)
            )
        else:
            settings = await onedrive.create_root_folder(
                services.http, tokens, default_onedrive_path(pending.chat_title)
            )
        await complete_setup(
            bot,
            services,
            chat_id=pending.chat_id,
            chat_title=pending.chat_title,
            user_id=pending.user_id,
            user_name=pending.user_name,
            provider=provider,
            credentials=tokens.credentials,
            settings=settings,
        )
    except (StorageError, SetupError) as exc:
        log.warning("OAuth setup for chat %s failed: %s", pending.chat_id, exc)
        await _tell_user(bot, pending.user_id, f"❌ No pude conectar {name}: {exc}")
        return page("No se pudo conectar", str(exc), status=502)
    return page(f"✅ {name} conectado", "Ya puedes cerrar esta ventana y volver a Telegram.")


def create_web_app(services: Services, bot: Bot) -> web.Application:
    app = web.Application()
    app[SERVICES_KEY] = services
    app[BOT_KEY] = bot
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/oauth/{client}/callback", oauth_callback)
    return app
