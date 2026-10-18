"""Who is allowed to change a chat's storage configuration."""

from __future__ import annotations

from telegram import Bot, Message, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

from ..config import Settings

ADMIN_STATUSES = {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return False
    return member.status in ADMIN_STATUSES


def may_configure(settings: Settings, user_id: int) -> bool:
    return not settings.allowed_configurators or user_id in settings.allowed_configurators


def is_anonymous_admin(message: Message) -> bool:
    return message.sender_chat is not None and message.sender_chat.id == message.chat_id


async def require_group_admin(
    update: Update, bot: Bot, settings: Settings, *, for_setup: bool = False
) -> bool:
    """Check the sender of a group command is an administrator, replying if they are not.

    ``for_setup`` additionally enforces ``ALLOWED_CONFIGURATORS``.
    """
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return False
    if is_anonymous_admin(message):
        await message.reply_text(
            "No puedo saber quién eres porque estás escribiendo como administrador anónimo. "
            "Desactiva temporalmente «Permanecer anónimo» en tus permisos y repite el comando."
        )
        return False
    if not await is_chat_admin(bot, message.chat_id, user.id):
        await message.reply_text("Solo los administradores del grupo pueden usar este comando.")
        return False
    if for_setup and not may_configure(settings, user.id):
        await message.reply_text("No tienes permiso para configurar este bot.")
        return False
    return True
