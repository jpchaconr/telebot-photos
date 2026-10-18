"""OAuth client factories for the providers that need a user authorization."""

from __future__ import annotations

import httpx

from ..config import Settings
from .base import OAuth2Client

GOOGLE = "google"
MICROSOFT = "microsoft"

# Storage provider -> OAuth client name.
OAUTH_FOR_PROVIDER = {"gdrive": GOOGLE, "onedrive": MICROSOFT}


def google_client(settings: Settings, http: httpx.AsyncClient) -> OAuth2Client:
    assert settings.google_client_id and settings.google_client_secret
    return OAuth2Client(
        name="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri=settings.redirect_uri(GOOGLE),
        # drive.file only grants access to the files and folders created by the bot.
        scopes=["https://www.googleapis.com/auth/drive.file"],
        http=http,
        extra_auth_params={
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        },
    )


def microsoft_client(settings: Settings, http: httpx.AsyncClient) -> OAuth2Client:
    assert settings.ms_client_id and settings.ms_client_secret
    base = f"https://login.microsoftonline.com/{settings.ms_tenant}/oauth2/v2.0"
    return OAuth2Client(
        name="Microsoft",
        authorize_url=f"{base}/authorize",
        token_url=f"{base}/token",
        client_id=settings.ms_client_id,
        client_secret=settings.ms_client_secret,
        redirect_uri=settings.redirect_uri(MICROSOFT),
        scopes=["offline_access", "Files.ReadWrite", "User.Read"],
        http=http,
        extra_auth_params={"response_mode": "query", "prompt": "select_account"},
        send_scope_on_refresh=True,
    )


def build_oauth_clients(settings: Settings, http: httpx.AsyncClient) -> dict[str, OAuth2Client]:
    clients: dict[str, OAuth2Client] = {}
    if settings.google_enabled:
        clients[GOOGLE] = google_client(settings, http)
    if settings.microsoft_enabled:
        clients[MICROSOFT] = microsoft_client(settings, http)
    return clients
