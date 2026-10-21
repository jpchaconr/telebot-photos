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
main.doc{{max-width:720px;text-align:left}}
h1{{font-size:1.4rem;margin:0 0 12px}}h2{{font-size:1.1rem;margin:24px 0 8px}}
p,li{{line-height:1.5}}p{{margin:0 0 12px}}a{{color:#0c66e4}}
@media (prefers-color-scheme:dark){{body{{background:#161a1d;color:#dee4ea}}
main{{background:#22272b}}a{{color:#579dff}}}}
</style></head>
<body><main class="{css_class}"><h1>{title}</h1>{content}</main></body></html>
"""


def html_page(
    title: str, content_html: str, *, status: int = 200, document: bool = False
) -> web.Response:
    """Render a page whose ``content_html`` is trusted markup built by this module."""
    return web.Response(
        text=PAGE.format(
            title=html.escape(title),
            content=content_html,
            css_class="doc" if document else "",
        ),
        status=status,
        content_type="text/html",
    )


def page(title: str, body: str, status: int = 200) -> web.Response:
    return html_page(title, f"<p>{html.escape(body)}</p>", status=status)


async def health(request: web.Request) -> web.Response:
    version = request.app[SERVICES_KEY].settings.app_version
    return web.json_response({"status": "ok", "version": version})


def _bot_link(bot: Bot) -> str:
    try:
        username = bot.username
    except RuntimeError:  # the bot has not called getMe yet
        return "el bot"
    return f'<a href="https://t.me/{html.escape(username)}">@{html.escape(username)}</a>'


async def index(request: web.Request) -> web.Response:
    bot = _bot_link(request.app[BOT_KEY])
    return html_page(
        "Bot de fotos para grupos de Telegram",
        f"<p>{bot} guarda automáticamente las fotos, vídeos y archivos que se envían en un "
        "grupo de Telegram en el almacenamiento que elige un administrador del grupo: "
        "Google Drive, OneDrive o un bucket compatible con S3.</p>"
        "<p>Para usarlo, añádelo a un grupo y escribe /configurar.</p>"
        '<p><a href="/privacy">Política de privacidad</a></p>',
    )


async def privacy(request: web.Request) -> web.Response:
    settings = request.app[SERVICES_KEY].settings
    contact = (
        f'Escribe a <a href="mailto:{html.escape(settings.contact_email)}">'
        f"{html.escape(settings.contact_email)}</a>."
        if settings.contact_email
        else "Contacta con el administrador del grupo que te invitó a usar el bot."
    )
    return html_page(
        "Política de privacidad",
        f"""
<p>Este bot copia el contenido multimedia de grupos de Telegram en el almacenamiento que
elige un administrador de cada grupo. Esta página explica qué datos trata y para qué.</p>
<h2>Qué datos se tratan</h2>
<ul>
<li><b>Configuración de cada grupo:</b> identificador y nombre del grupo, quién lo configuró,
el proveedor elegido y sus credenciales (tokens OAuth de Google o Microsoft, o claves S3).
Las credenciales se guardan cifradas.</li>
<li><b>Archivos enviados al grupo:</b> identificadores de Telegram del mensaje, del archivo y
del remitente, nombre y nombre de usuario del remitente, fecha y pie de foto, para nombrar
cada copia, anotar en su descripción o metadatos quién la envió y con qué comentario, y evitar
duplicados. Los archivos se descargan temporalmente en este servidor solo para subirlos, y se
borran después.</li>
</ul>
<h2>Para qué se usan</h2>
<p>Únicamente para copiar los archivos del grupo a la cuenta de almacenamiento que eligió su
administrador. No se usan con fines publicitarios, no se venden y no se ceden a terceros.</p>
<h2>Datos de Google</h2>
<p>El bot solo pide el permiso <code>drive.file</code>: puede crear carpetas y archivos en tu
Google Drive y acceder únicamente a los que él mismo ha creado. El uso y la transferencia de
la información recibida de las API de Google cumplen la
<a href="https://developers.google.com/terms/api-services-user-data-policy">Política de
datos de usuario de los servicios de las API de Google</a>, incluidos los requisitos de uso
limitado (Limited Use).</p>
<h2>Datos de Microsoft</h2>
<p>Con OneDrive, el bot usa los permisos <code>Files.ReadWrite</code>,
<code>offline_access</code> y <code>User.Read</code> solo para crear la carpeta del grupo y
subir los archivos.</p>
<h2>Conservación y borrado</h2>
<p>Un administrador puede usar /desconectar en el grupo para borrar las credenciales y la cola
de archivos pendientes. También puedes revocar el acceso desde tu cuenta de
<a href="https://myaccount.google.com/permissions">Google</a> o de
<a href="https://account.live.com/consent/Manage">Microsoft</a>. Los archivos ya copiados
están en tu cuenta y el bot no los borra.</p>
<h2>Contacto</h2>
<p>{contact}</p>
""",
        document=True,
    )


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
    app.router.add_get("/privacy", privacy)
    app.router.add_get("/health", health)
    app.router.add_get("/oauth/{client}/callback", oauth_callback)
    return app
