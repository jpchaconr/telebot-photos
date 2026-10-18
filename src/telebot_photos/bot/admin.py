"""Group commands: status, folder link, pause/resume, disconnect, help and housekeeping."""

from __future__ import annotations

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

from ..services import get_services
from ..storage.base import PROVIDER_NAMES
from .permissions import is_chat_admin, require_group_admin
from .setup import describe_location
from .texts import WELCOME, help_text

log = logging.getLogger(__name__)

NOT_CONFIGURED = "Este grupo aún no tiene almacenamiento. Un administrador puede usar /configurar."


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
    chat = await get_services(context).db.get_chat(message.chat_id)
    if chat is None:
        await message.reply_text(NOT_CONFIGURED)
        return
    url = chat.settings.get("folder_url")
    if url:
        await message.reply_text(
            f"📁 Carpeta en {PROVIDER_NAMES.get(chat.provider)}: {url}\n"
            "(Solo podrá abrirla quien tenga acceso a esa cuenta o a quien se la compartan.)"
        )
    else:
        await message.reply_text(
            f"📁 Los archivos se guardan en {describe_location(chat.provider, chat.settings)}"
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
        CommandHandler(["pausar", "pause"], pause_command),
        CommandHandler(["reanudar", "resume"], resume_command),
        CommandHandler(["desconectar", "disconnect"], disconnect_command),
        CallbackQueryHandler(disconnect_callback, pattern=r"^disconnect:"),
        CallbackQueryHandler(stale_setup_callback, pattern=r"^setup:"),
        MessageHandler(filters.StatusUpdate.MIGRATE, migrate_handler),
        ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER),
    ]
