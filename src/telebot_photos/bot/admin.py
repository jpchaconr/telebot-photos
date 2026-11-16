"""Group commands: status, folder link, gallery, pause/resume, disconnect, help and housekeeping."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime

from telegram import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .. import thumbs
from ..db import ChatConfig
from ..services import Services, get_services
from ..storage.base import PROVIDER_NAMES, StorageAuthError, StorageError
from ..storage.registry import storage_location
from ..storage.s3 import S3Backend
from .permissions import is_anonymous_admin, is_chat_admin, require_group_admin
from .setup import describe_location
from .texts import WELCOME, help_text

log = logging.getLogger(__name__)

NOT_CONFIGURED = "Este grupo aún no tiene almacenamiento. Un administrador puede usar /configurar."
# Arguments of /enlace that open the folder to anyone with the link or close it again.
PUBLIC_OPTIONS = {"publico", "público", "public"}
PRIVATE_OPTIONS = {"privado", "private"}
GALLERY_OFF = (
    "La galería web de este grupo no está activada. Un administrador puede activarla con /galeria."
)
GALLERY_BUTTONS = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("🔄 Cambiar enlace", callback_data="gallery:rotate"),
            InlineKeyboardButton("🚫 Desactivar", callback_data="gallery:off"),
        ]
    ]
)


def _in_group(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            help_text(get_services(context)), parse_mode=ParseMode.HTML
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not _in_group(update):
        return
    services = get_services(context)
    chat = await services.db.get_chat(message.chat_id)
    if chat is None:
        await message.reply_text(NOT_CONFIGURED)
        return
    stats = await services.db.job_stats(chat.chat_id)
    if chat.status == "reauth_required":
        state = "⚠️ hay que reconectar la cuenta (/configurar)"
    elif chat.paused:
        state = "⏸ en pausa"
    else:
        state = "▶️ activo"
    configured_at = datetime.fromtimestamp(chat.configured_at, services.settings.tz)
    location = html.escape(describe_location(chat.provider, chat.settings))
    url = chat.settings.get("folder_url")
    if url:
        location = f'<a href="{html.escape(url)}">{location}</a>'
    await message.reply_text(
        f"📦 <b>{PROVIDER_NAMES.get(chat.provider, chat.provider)}</b> · {location}\n"
        f"👤 Configurado por {html.escape(chat.configured_by_name)} el "
        f"{configured_at:%d/%m/%Y}\n"
        f"Estado: {state}\n\n"
        f"✅ Guardados: {stats['done']}\n"
        f"⏳ Pendientes: {stats['pending'] + stats['in_progress']}\n"
        f"❌ Con error: {stats['failed']}",
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not _in_group(update):
        return
    if context.args:
        await _set_link_access(update, context, context.args[0].lower())
        return
    chat = await get_services(context).db.get_chat(message.chat_id)
    if chat is None:
        await message.reply_text(NOT_CONFIGURED)
        return
    url = chat.settings.get("folder_url")
    if url and chat.provider == "gdrive":
        await message.reply_text(
            f"📁 Carpeta en {PROVIDER_NAMES['gdrive']}: {url}\n"
            "(Si pide acceso, un administrador puede abrirla en solo lectura con /enlace publico.)"
        )
    elif url:
        await message.reply_text(
            f"📁 Carpeta en {PROVIDER_NAMES.get(chat.provider)}: {url}\n"
            "(Solo podrá abrirla quien tenga acceso a esa cuenta o a quien se la compartan.)"
        )
    else:
        await message.reply_text(
            f"📁 Los archivos se guardan en {describe_location(chat.provider, chat.settings)}"
        )


async def _set_link_access(update: Update, context: ContextTypes.DEFAULT_TYPE, option: str) -> None:
    """/enlace publico | privado: open the Drive folder to anyone with the link, read only."""
    message = update.effective_message
    if message is None:
        return
    if option not in PUBLIC_OPTIONS | PRIVATE_OPTIONS:
        await message.reply_text(
            "Usa /enlace para ver el enlace de la carpeta, /enlace publico para que cualquiera "
            "con el enlace pueda verla (solo lectura) o /enlace privado para cerrarla."
        )
        return
    services = get_services(context)
    if not await require_group_admin(update, context.bot, services.settings):
        return
    chat = await services.db.get_chat(message.chat_id)
    if chat is None:
        await message.reply_text(NOT_CONFIGURED)
        return
    if chat.provider != "gdrive":
        await message.reply_text(
            "Solo puedo abrir o cerrar el enlace de la carpeta con Google Drive, y este grupo "
            f"usa {PROVIDER_NAMES.get(chat.provider, chat.provider)}."
        )
        return
    public = option in PUBLIC_OPTIONS
    try:
        await services.backends.get(chat).set_public_link(public)
    except StorageError as exc:
        log.warning("Could not change link sharing for chat %s: %s", chat.chat_id, exc)
        await message.reply_text(f"❌ No pude cambiar el acceso a la carpeta: {exc}")
        return
    if public:
        await message.reply_text(
            "🔓 Carpeta abierta en solo lectura: cualquiera con el enlace puede ver los "
            "archivos, pero no cambiarlos ni borrarlos. También quien reciba el enlace fuera "
            f"del grupo.\n📁 {chat.settings.get('folder_url')}\n\n"
            "Para volver a cerrarla: /enlace privado"
        )
    else:
        await message.reply_text(
            "🔒 Carpeta cerrada: solo podrá abrirla quien tenga acceso a la cuenta o a quien "
            "se la compartan.\n\nPara abrirla en solo lectura: /enlace publico"
        )


def gallery_text(services: Services, token: str, note: str | None = None) -> str:
    url = f"{services.settings.public_base_url}/g/{token}"
    text = (
        f"🖼 Galería del grupo: {url}\n\n"
        "Ahí se ven las fotos, vídeos y GIFs guardados. Quien tenga el enlace puede verla: "
        "no lo compartas fuera del grupo. Los administradores pueden cambiarlo o desactivar "
        "la galería con los botones."
    )
    return f"{note}\n\n{text}" if note else text


async def _gallery_warning(services: Services, chat: ChatConfig) -> str | None:
    """Check the gallery will be able to read the files: S3 keys may only allow writing."""
    if chat.provider != "s3":
        return None
    backend = services.backends.get(chat)
    if not isinstance(backend, S3Backend):
        return None
    latest = await services.db.gallery_items(chat.chat_id, storage_location(chat), limit=1)
    if not latest:
        return None
    try:
        await backend.check_read(latest[0].remote_ref)
    except StorageAuthError:
        return (
            "⚠️ Las claves de S3 no tienen permiso de lectura (s3:GetObject) en el bucket: "
            "la galería no podrá mostrar las fotos hasta que se lo des."
        )
    except StorageError as exc:
        log.info("Could not check S3 read access for chat %s: %s", chat.chat_id, exc)
    return None


async def gallery_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not _in_group(update):
        await message.reply_text("Usa /galeria dentro del grupo cuyas fotos quieres ver.")
        return
    services = get_services(context)
    chat = await services.db.get_chat(message.chat_id)
    if chat is None:
        await message.reply_text(NOT_CONFIGURED)
        return
    token = await services.db.get_gallery_token(chat.chat_id)
    note = None
    if token is None:
        # Only an administrator can publish the gallery; anyone can ask for the link later.
        user = update.effective_user
        if is_anonymous_admin(message):
            await require_group_admin(update, context.bot, services.settings)  # explains why
            return
        if user is None or not await is_chat_admin(context.bot, chat.chat_id, user.id):
            await message.reply_text(GALLERY_OFF)
            return
        token = await services.db.create_gallery(chat.chat_id, user.id)
        note = await _gallery_warning(services, chat) or "✅ Galería activada."
    await message.reply_text(
        gallery_text(services, token, note),
        reply_markup=GALLERY_BUTTONS,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        # Members can't forward the link out of the group from Telegram.
        protect_content=True,
    )


async def gallery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    chat_id = query.message.chat.id
    services = get_services(context)
    if not await is_chat_admin(context.bot, chat_id, query.from_user.id):
        await query.answer("Solo los administradores pueden hacer esto.", show_alert=True)
        return
    await query.answer()
    if query.data == "gallery:off":
        await services.db.delete_gallery(chat_id)
        await asyncio.to_thread(thumbs.delete_chat, services.settings, chat_id)
        await query.edit_message_text(
            "🚫 Galería desactivada: el enlace ya no funciona. "
            "Un administrador puede volver a activarla con /galeria."
        )
        return
    if await services.db.get_chat(chat_id) is None:
        await query.edit_message_text(NOT_CONFIGURED)
        return
    token = await services.db.create_gallery(chat_id, query.from_user.id)
    await query.edit_message_text(
        gallery_text(services, token, "🔄 Enlace nuevo: el anterior ya no funciona."),
        reply_markup=GALLERY_BUTTONS,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def _set_paused(update: Update, context: ContextTypes.DEFAULT_TYPE, paused: bool) -> None:
    message = update.effective_message
    if message is None or not _in_group(update):
        return
    services = get_services(context)
    if not await require_group_admin(update, context.bot, services.settings):
        return
    if not await services.db.set_paused(message.chat_id, paused):
        await message.reply_text(NOT_CONFIGURED)
        return
    await message.reply_text(
        "⏸ Guardado en pausa. Los archivos que se envíen ahora no se guardarán."
        if paused
        else "▶️ Guardado reanudado."
    )


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_paused(update, context, True)


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_paused(update, context, False)


async def disconnect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not _in_group(update):
        return
    services = get_services(context)
    if not await require_group_admin(update, context.bot, services.settings):
        return
    if await services.db.get_chat(message.chat_id) is None:
        await message.reply_text(NOT_CONFIGURED)
        return
    await message.reply_text(
        "¿Seguro que quieres desconectar el almacenamiento? Dejaré de guardar archivos y "
        "borraré las credenciales y la cola pendiente. Los archivos ya guardados no se tocan.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Sí, desconectar", callback_data="disconnect:yes"),
                    InlineKeyboardButton("No", callback_data="disconnect:no"),
                ]
            ]
        ),
    )


async def disconnect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    chat_id = query.message.chat.id
    services = get_services(context)
    if not await is_chat_admin(context.bot, chat_id, query.from_user.id):
        await query.answer("Solo los administradores pueden hacer esto.", show_alert=True)
        return
    await query.answer()
    if query.data != "disconnect:yes":
        await query.edit_message_text("Cancelado: el almacenamiento sigue conectado.")
        return
    await services.db.delete_chat(chat_id)
    services.backends.invalidate(chat_id)
    await asyncio.to_thread(thumbs.delete_chat, services.settings, chat_id)
    await query.edit_message_text(
        "🔌 Almacenamiento desconectado. Usa /configurar para conectar uno nuevo."
    )


async def stale_setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is not None:
        await query.answer(
            "Esta configuración ya caducó. Vuelve a usar /configurar en el grupo.",
            show_alert=True,
        )


async def migrate_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.migrate_to_chat_id is None:
        return
    log.info("Chat %s migrated to %s", message.chat_id, message.migrate_to_chat_id)
    services = get_services(context)
    await services.db.migrate_chat(message.chat_id, message.migrate_to_chat_id)
    services.backends.invalidate(message.chat_id)
    await asyncio.to_thread(
        thumbs.move_chat, services.settings, message.chat_id, message.migrate_to_chat_id
    )


async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change: ChatMemberUpdated | None = update.my_chat_member
    if change is None or change.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    was_in = change.old_chat_member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
    is_in = change.new_chat_member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
    if is_in and not was_in:
        log.info("Added to chat %s (%s)", change.chat.id, change.chat.title)
        services = get_services(context)
        text = WELCOME
        if await services.db.get_chat(change.chat.id):
            text = "👋 ¡He vuelto! Sigo usando el almacenamiento configurado. Mira /estado."
        try:
            await context.bot.send_message(change.chat.id, text)
        except TelegramError as exc:
            log.warning("Could not greet chat %s: %s", change.chat.id, exc)
    elif was_in and not is_in:
        log.info("Removed from chat %s", change.chat.id)


def build_handlers() -> list:
    return [
        CommandHandler(["ayuda", "help"], help_command),
        CommandHandler("start", help_command, filters.ChatType.GROUPS),
        CommandHandler(["estado", "status"], status_command),
        CommandHandler(["enlace", "link"], link_command),
        CommandHandler(["galeria", "gallery"], gallery_command),
        CommandHandler(["pausar", "pause"], pause_command),
        CommandHandler(["reanudar", "resume"], resume_command),
        CommandHandler(["desconectar", "disconnect"], disconnect_command),
        CallbackQueryHandler(disconnect_callback, pattern=r"^disconnect:"),
        CallbackQueryHandler(gallery_callback, pattern=r"^gallery:"),
        CallbackQueryHandler(stale_setup_callback, pattern=r"^setup:"),
        MessageHandler(filters.StatusUpdate.MIGRATE, migrate_handler),
        ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER),
    ]
