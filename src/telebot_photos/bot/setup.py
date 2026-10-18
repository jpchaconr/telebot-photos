"""Per-chat storage setup: /configurar in the group, then a private conversation."""

from __future__ import annotations

import html
import logging
import re
from typing import Any
from urllib.parse import urlparse

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from ..naming import sanitize
from ..oauth.providers import OAUTH_FOR_PROVIDER
from ..services import Services, get_services
from ..storage.base import PROVIDER_NAMES, StorageError
from ..storage.s3 import S3Backend
from .permissions import is_chat_admin, require_group_admin
from .texts import help_text

log = logging.getLogger(__name__)

SETUP_PREFIX = "setup_"
CHOOSE, S3_ENDPOINT, S3_REGION, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET, S3_PREFIX = range(7)
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-_]{1,253}[a-z0-9]$")


class SetupError(Exception):
    pass


def display_name(user: User) -> str:
    return f"@{user.username}" if user.username else user.full_name


def default_folder_name(chat_title: str) -> str:
    return f"Telegram - {sanitize(chat_title, 60, 'grupo')}"


def default_onedrive_path(chat_title: str) -> str:
    return f"Telegram/{sanitize(chat_title, 60, 'grupo')}"


def describe_location(provider: str, settings: dict[str, Any]) -> str:
    if provider == "gdrive":
        return f"carpeta «{settings.get('folder_name', '')}»"
    if provider == "onedrive":
        return f"carpeta «{settings.get('root_path', '')}»"
    if provider == "s3":
        prefix = settings.get("prefix", "")
        return f"s3://{settings.get('bucket', '')}/{prefix}".rstrip("/")
    return provider


async def complete_setup(
    bot: Bot,
    services: Services,
    *,
    chat_id: int,
    chat_title: str,
    user_id: int,
    user_name: str,
    provider: str,
    credentials: dict[str, Any],
    settings: dict[str, Any],
) -> None:
    """Store a finished configuration and announce it in the group and in private."""
    if not await is_chat_admin(bot, chat_id, user_id):
        raise SetupError("ya no eres administrador del grupo")
    await services.db.save_chat(
        chat_id=chat_id,
        title=chat_title,
        provider=provider,
        credentials=credentials,
        settings=settings,
        configured_by=user_id,
        configured_by_name=user_name,
    )
    services.backends.invalidate(chat_id)
    services.worker.notify()

    name = PROVIDER_NAMES[provider]
    location = html.escape(describe_location(provider, settings))
    try:
        await bot.send_message(
            chat_id,
            f"📸 ¡Listo! Desde ahora guardo las fotos, vídeos y archivos de este grupo en "
            f"<b>{name}</b> ({location}).\nConfigurado por {html.escape(user_name)}. "
            "Usa /estado para ver el progreso.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        log.warning("Could not announce the setup in chat %s: %s", chat_id, exc)
    try:
        await bot.send_message(
            user_id,
            f"✅ «{html.escape(chat_title)}» está conectado a <b>{name}</b> ({location}).",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        log.warning("Could not confirm the setup to user %s: %s", user_id, exc)


# --- group side --------------------------------------------------------------------------


async def configure_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    if chat.type == ChatType.PRIVATE:
        await message.reply_text("Usa /configurar dentro del grupo cuyo contenido quieres guardar.")
        return
    services = get_services(context)
    if not await require_group_admin(update, context.bot, services.settings, for_setup=True):
        return
    token = await services.db.create_setup_session(
        chat_id=chat.id, chat_title=chat.title or str(chat.id), user_id=user.id
    )
    url = f"https://t.me/{context.bot.username}?start={SETUP_PREFIX}{token}"
    current = await services.db.get_chat(chat.id)
    text = (
        "Para conectar el almacenamiento continúa en el chat privado conmigo, así las "
        "credenciales no se escriben en el grupo. El enlace solo funciona para ti y caduca "
        "en 15 minutos."
    )
    if current:
        text = (
            f"Este grupo guarda actualmente en {PROVIDER_NAMES.get(current.provider)}. "
            "Si continúas, la configuración se reemplazará.\n\n" + text
        )
    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⚙️ Configurar en privado", url=url)]]
        ),
    )


# --- private conversation ----------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return ConversationHandler.END
    args = context.args or []
    if not args or not args[0].startswith(SETUP_PREFIX):
        await message.reply_text(help_text(get_services(context)), parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    services = get_services(context)
    session = await services.db.consume_setup_session(args[0][len(SETUP_PREFIX) :], user.id)
    if session is None:
        await message.reply_text(
            "Este enlace no es válido o ha caducado. Vuelve a usar /configurar en el grupo."
        )
        return ConversationHandler.END
    if not await is_chat_admin(context.bot, session.chat_id, user.id):
        await message.reply_text("Ya no eres administrador de ese grupo.")
        return ConversationHandler.END

    context.user_data["setup"] = {"chat_id": session.chat_id, "chat_title": session.chat_title}
    buttons = [
        [InlineKeyboardButton(PROVIDER_NAMES[p], callback_data=f"setup:{p}")]
        for p in services.available_providers()
    ]
    buttons.append([InlineKeyboardButton("Cancelar", callback_data="setup:cancel")])
    await message.reply_text(
        f"¿Dónde quieres guardar la multimedia de «{session.chat_title}»?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return CHOOSE


async def choose_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return ConversationHandler.END
    await query.answer()
    setup = context.user_data.get("setup")
    provider = (query.data or "").partition(":")[2]
    if not setup or provider == "cancel":
        context.user_data.pop("setup", None)
        await query.edit_message_text("Configuración cancelada.")
        return ConversationHandler.END

    services = get_services(context)
    if provider in OAUTH_FOR_PROVIDER and provider in services.available_providers():
        client = services.oauth[OAUTH_FOR_PROVIDER[provider]]
        state = await services.db.create_oauth_state(
            provider=provider,
            chat_id=setup["chat_id"],
            chat_title=setup["chat_title"],
            user_id=user.id,
            user_name=display_name(user),
        )
        context.user_data.pop("setup", None)
        if provider == "gdrive":
            detail = (
                f"Crearé la carpeta «{default_folder_name(setup['chat_title'])}» en tu Drive. "
                "El bot solo tendrá acceso a los archivos que él mismo cree."
            )
        else:
            detail = (
                f"Guardaré todo en la carpeta «{default_onedrive_path(setup['chat_title'])}» "
                "de tu OneDrive."
            )
        await query.edit_message_text(
            f"Pulsa el botón para autorizar el acceso a tu cuenta de {PROVIDER_NAMES[provider]}."
            f"\n\n{detail}\n\nCuando termines te avisaré aquí y en el grupo. "
            "El enlace caduca en 15 minutos.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔐 Autorizar", url=client.authorization_url(state))]]
            ),
        )
        return ConversationHandler.END

    if provider == "s3":
        setup["s3"] = {}
        await query.edit_message_text(
            "Vamos a conectar un bucket compatible con S3 (AWS S3, Cloudflare R2, Backblaze B2, "
            "MinIO…). Puedes cancelar en cualquier momento con /cancelar.\n\n"
            "1/6 · Envía la URL del endpoint (por ejemplo "
            "https://<cuenta>.r2.cloudflarestorage.com) o escribe «aws» si es Amazon S3."
        )
        return S3_ENDPOINT

    await query.edit_message_text("Proveedor no disponible.")
    return ConversationHandler.END


async def _delete_secret(update: Update) -> None:
    if update.effective_message is None:
        return
    try:
        await update.effective_message.delete()
    except TelegramError:
        log.debug("Could not delete a message containing a secret")


def _s3_data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any] | None:
    setup = context.user_data.get("setup")
    return setup.get("s3") if setup else None


async def s3_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _s3_data(context)
    message = update.effective_message
    if data is None or message is None or message.text is None:
        return ConversationHandler.END
    text = message.text.strip()
    if text.lower() in {"aws", "amazon", "s3"}:
        data["endpoint_url"] = None
    else:
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            await message.reply_text(
                "Eso no parece una URL válida. Envíala con https://, o escribe «aws»."
            )
            return S3_ENDPOINT
        data["endpoint_url"] = text.rstrip("/")
    await message.reply_text(
        "2/6 · ¿En qué región está el bucket? (por ejemplo us-east-1 o eu-west-1; "
        "para Cloudflare R2 escribe «auto»)."
    )
    return S3_REGION


async def s3_region(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _s3_data(context)
    message = update.effective_message
    if data is None or message is None or message.text is None:
        return ConversationHandler.END
    data["region"] = message.text.strip().lower()
    await message.reply_text("3/6 · ¿Cómo se llama el bucket?")
    return S3_BUCKET


async def s3_bucket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _s3_data(context)
    message = update.effective_message
    if data is None or message is None or message.text is None:
        return ConversationHandler.END
    bucket = message.text.strip()
    if not _BUCKET_RE.match(bucket):
        await message.reply_text(
            "Ese nombre de bucket no es válido (solo minúsculas, números, puntos y guiones)."
        )
        return S3_BUCKET
    data["bucket"] = bucket
    await message.reply_text("4/6 · Envía el Access Key ID. Borraré tu mensaje en cuanto lo lea.")
    return S3_ACCESS_KEY


async def s3_access_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _s3_data(context)
    message = update.effective_message
    if data is None or message is None or message.text is None:
        return ConversationHandler.END
    data["access_key_id"] = message.text.strip()
    await _delete_secret(update)
    await message.chat.send_message(
        "5/6 · Ahora envía el Secret Access Key. También borraré ese mensaje."
    )
    return S3_SECRET


async def s3_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _s3_data(context)
    message = update.effective_message
    if data is None or message is None or message.text is None:
        return ConversationHandler.END
    data["secret_access_key"] = message.text.strip()
    await _delete_secret(update)
    setup = context.user_data["setup"]
    data["default_prefix"] = f"telegram/{sanitize(setup['chat_title'], 60, 'grupo')}"
    await message.chat.send_message(
        "6/6 · ¿En qué carpeta (prefijo) del bucket guardo los archivos? "
        f"Envía «-» para usar «{data['default_prefix']}», o «/» para la raíz del bucket."
    )
    return S3_PREFIX


async def s3_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _s3_data(context)
    message = update.effective_message
    user = update.effective_user
    if data is None or message is None or message.text is None or user is None:
        return ConversationHandler.END
    text = message.text.strip()
    if text == "-":
        prefix = data["default_prefix"]
    else:
        prefix = "/".join(sanitize(part, 60) for part in text.split("/") if part.strip())
    setup = context.user_data.pop("setup")
    credentials = {
        "access_key_id": data["access_key_id"],
        "secret_access_key": data["secret_access_key"],
    }
    settings = {
        "endpoint_url": data["endpoint_url"],
        "region": data["region"],
        "bucket": data["bucket"],
        "prefix": prefix,
    }
    await message.reply_text("Comprobando que puedo escribir en el bucket…")
    backend = S3Backend(credentials, settings)
    try:
        await backend.check()
    except StorageError as exc:
        await message.reply_text(
            f"❌ No pude escribir en el bucket: {exc}\n\n"
            "Revisa los datos y vuelve a empezar con /configurar en el grupo."
        )
        return ConversationHandler.END
    finally:
        await backend.aclose()

    try:
        await complete_setup(
            context.bot,
            get_services(context),
            chat_id=setup["chat_id"],
            chat_title=setup["chat_title"],
            user_id=user.id,
            user_name=display_name(user),
            provider="s3",
            credentials=credentials,
            settings=settings,
        )
    except SetupError as exc:
        await message.reply_text(f"❌ No se guardó la configuración: {exc}.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("setup", None)
    if update.effective_message:
        await update.effective_message.reply_text("Configuración cancelada.")
    return ConversationHandler.END


def build_handlers() -> list:
    text = filters.TEXT & ~filters.COMMAND
    conversation = ConversationHandler(
        entry_points=[CommandHandler("start", start_command, filters.ChatType.PRIVATE)],
        states={
            CHOOSE: [CallbackQueryHandler(choose_provider, pattern=r"^setup:")],
            S3_ENDPOINT: [MessageHandler(text, s3_endpoint)],
            S3_REGION: [MessageHandler(text, s3_region)],
            S3_BUCKET: [MessageHandler(text, s3_bucket)],
            S3_ACCESS_KEY: [MessageHandler(text, s3_access_key)],
            S3_SECRET: [MessageHandler(text, s3_secret)],
            S3_PREFIX: [MessageHandler(text, s3_prefix)],
        },
        fallbacks=[CommandHandler("cancelar", cancel)],
        allow_reentry=True,
    )
    return [
        conversation,
        CommandHandler(["configurar", "setup"], configure_command),
    ]
