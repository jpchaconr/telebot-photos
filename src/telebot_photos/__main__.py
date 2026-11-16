"""Entry point: runs the Telegram bot, the OAuth web server and the upload workers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import warnings

import httpx
from aiohttp import web
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    Update,
)
from telegram.error import BadRequest, NetworkError
from telegram.ext import Application, ApplicationBuilder, ContextTypes
from telegram.warnings import PTBUserWarning

from .bot import admin, media, setup
from .config import Settings
from .crypto import Crypto
from .db import Database
from .oauth.providers import build_oauth_clients
from .services import Services
from .storage.registry import BackendRegistry
from .web import create_web_app
from .worker import UploadWorker

log = logging.getLogger("telebot_photos")

GROUP_COMMANDS = [
    BotCommand("configurar", "Conectar o cambiar el almacenamiento (admins)"),
    BotCommand("estado", "Dónde se guarda y cuántos archivos llevo"),
    BotCommand("enlace", "Enlace a la carpeta"),
    BotCommand("galeria", "Ver las fotos del grupo en la web"),
    BotCommand("pausar", "Pausar el guardado (admins)"),
    BotCommand("reanudar", "Reanudar el guardado (admins)"),
    BotCommand("desconectar", "Olvidar el almacenamiento (admins)"),
    BotCommand("ayuda", "Ayuda"),
]
PRIVATE_COMMANDS = [
    BotCommand("ayuda", "Ayuda"),
    BotCommand("cancelar", "Cancelar la configuración en curso"),
]


def build_application(settings: Settings) -> Application:
    builder = ApplicationBuilder().token(settings.telegram_bot_token)
    if settings.bot_api_url:
        builder = (
            builder.base_url(f"{settings.bot_api_url}/bot")
            .base_file_url(f"{settings.bot_api_url}/file/bot")
            .local_mode(True)
        )
    return builder.build()


def register_handlers(application: Application) -> None:
    # The private setup conversation must come first so it sees /start setup_<token>.
    for handler in setup.build_handlers() + admin.build_handlers() + media.build_handlers():
        application.add_handler(handler)
    application.add_error_handler(_on_error)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error handling update %s", update, exc_info=context.error)


async def initialize_with_retry(
    application: Application,
    stop: asyncio.Event,
    *,
    attempts: int = 10,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
) -> bool:
    """Call ``getMe`` until the Bot API answers (the local server may still be starting).

    Returns False if ``stop`` was set while waiting. Gives up (re-raising) after about three
    minutes so the container restarts with a clear error.
    """
    for attempt in range(1, attempts + 1):
        try:
            await application.initialize()
            return True
        except NetworkError as exc:
            if isinstance(exc, BadRequest) or attempt == attempts:
                raise
            delay = min(base_delay * 2 ** (attempt - 1), max_delay)
            log.warning("The Bot API is not reachable yet (%s); retrying in %.0fs", exc, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)
            if stop.is_set():
                return False
    return False


async def serve(settings: Settings, stop: asyncio.Event) -> None:
    """Run the bot, the web server and the workers until ``stop`` is set."""
    db = Database(settings.database_path, Crypto(settings.encryption_key))
    await db.connect()
    http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15))
    oauth = build_oauth_clients(settings, http)
    backends = BackendRegistry(db, http, oauth)

    application = build_application(settings)
    worker = UploadWorker(application.bot, db, backends, settings)
    services = Services(settings, db, http, oauth, backends, worker)
    application.bot_data["services"] = services
    register_handlers(application)

    runner = web.AppRunner(create_web_app(services, application.bot))
    try:
        if not await initialize_with_retry(application, stop):
            return
        try:
            await application.bot.set_my_commands(GROUP_COMMANDS, BotCommandScopeAllGroupChats())
            await application.bot.set_my_commands(
                PRIVATE_COMMANDS, BotCommandScopeAllPrivateChats()
            )
            await application.start()
            await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            await runner.setup()
            await web.TCPSite(runner, settings.web_host, settings.web_port).start()
            await worker.start()
            log.info(
                "Bot @%s running (version %s, Bot API: %s, providers: %s)",
                application.bot.username,
                settings.app_version,
                settings.bot_api_url or "cloud",
                ", ".join(services.available_providers()),
            )
            await stop.wait()
            log.info("Shutting down")
            await worker.stop()
            await application.updater.stop()
            await application.stop()
        finally:
            await application.shutdown()
    finally:
        await runner.cleanup()
        await backends.aclose()
        await http.aclose()
        await db.close()


async def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    warnings.filterwarnings("ignore", message="If 'per_message=False'", category=PTBUserWarning)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await serve(settings, stop)


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
