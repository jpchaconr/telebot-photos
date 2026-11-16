"""Longer user-facing texts."""

from __future__ import annotations

from ..services import Services
from ..storage.base import PROVIDER_NAMES


def help_text(services: Services) -> str:
    providers = ", ".join(PROVIDER_NAMES[p] for p in services.available_providers())
    return (
        "📸 <b>Bot para guardar la multimedia del grupo</b>\n\n"
        "Añádeme a un grupo y guardaré automáticamente las fotos, vídeos, GIFs, audios, "
        "notas de voz y documentos que se envíen, en la nube que elija un administrador "
        f"({providers}).\n\n"
        "<b>Comandos en el grupo</b>\n"
        "/configurar – conectar o cambiar el almacenamiento (admins)\n"
        "/estado – dónde se guarda y cuántos archivos llevo\n"
        "/enlace – enlace a la carpeta\n"
        "/enlace publico y /enlace privado – abrir la carpeta de Google Drive en solo lectura "
        "a cualquiera con el enlace, o cerrarla (admins)\n"
        "/galeria – ver las fotos y vídeos del grupo en una web (la activa un admin)\n"
        "/pausar y /reanudar – detener o reanudar el guardado (admins)\n"
        "/desconectar – olvidar el almacenamiento de este grupo (admins)\n"
        "/ayuda – este mensaje\n\n"
        "Solo guardo lo que se envía mientras estoy en el grupo; no puedo recuperar "
        "mensajes anteriores."
    )


WELCOME = (
    "👋 ¡Hola! Guardaré en la nube las fotos, vídeos y archivos que se envíen en este grupo.\n\n"
    "Un administrador debe usar /configurar para elegir dónde (Google Drive, OneDrive o S3). "
    "Usa /ayuda para ver todos los comandos."
)
