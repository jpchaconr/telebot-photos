"""Container for the long-lived objects shared by handlers, the worker and the web server."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from .config import Settings
from .db import Database
from .oauth.base import OAuth2Client
from .oauth.providers import OAUTH_FOR_PROVIDER
from .storage.registry import BackendRegistry

if TYPE_CHECKING:
    from .worker import UploadWorker


@dataclass
class Services:
    settings: Settings
    db: Database
    http: httpx.AsyncClient
    oauth: dict[str, OAuth2Client]
    backends: BackendRegistry
    worker: UploadWorker

    def available_providers(self) -> list[str]:
        """Storage providers a chat can choose (OAuth ones need their app credentials)."""
        oauth_ready = [p for p, client in OAUTH_FOR_PROVIDER.items() if client in self.oauth]
        return [*oauth_ready, "s3"]


def get_services(context: Any) -> Services:
    """Fetch the services from a PTB ``CallbackContext`` (stored in ``bot_data``)."""
    return context.bot_data["services"]
