import pytest
from cryptography.fernet import Fernet, InvalidToken

from telebot_photos.config import Settings
from telebot_photos.crypto import Crypto


def test_settings_parse_ids_and_strip_urls():
    settings = Settings(
        _env_file=None,
        telegram_bot_token="t",
        public_base_url="https://bot.example.com/",
        encryption_key=Fernet.generate_key().decode(),
        allowed_configurators="1, 2;3",
    )
    assert settings.allowed_configurators == [1, 2, 3]
    assert settings.redirect_uri("google") == "https://bot.example.com/oauth/google/callback"
    assert not settings.local_bot_api
    assert not settings.google_enabled


def test_settings_reject_unknown_timezone():
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            telegram_bot_token="t",
            public_base_url="https://x",
            encryption_key=Fernet.generate_key().decode(),
            timezone="Mars/Olympus",
        )


def test_crypto_round_trip_and_wrong_key():
    crypto = Crypto(Crypto.generate_key())
    token = crypto.encrypt_json({"secret": "s3cr3t"})
    assert b"s3cr3t" not in token
    assert crypto.decrypt_json(token) == {"secret": "s3cr3t"}
    with pytest.raises(InvalidToken):
        Crypto(Crypto.generate_key()).decrypt_json(token)


def test_public_url_is_derived_from_domain():
    key = Fernet.generate_key().decode()
    settings = Settings(
        _env_file=None, telegram_bot_token="t", domain="fotos.duckdns.org", encryption_key=key
    )
    assert settings.public_base_url == "https://fotos.duckdns.org"
    assert settings.redirect_uri("microsoft") == (
        "https://fotos.duckdns.org/oauth/microsoft/callback"
    )
    explicit = Settings(
        _env_file=None,
        telegram_bot_token="t",
        domain="fotos.duckdns.org",
        public_base_url="https://other.example.com/",
        encryption_key=key,
    )
    assert explicit.public_base_url == "https://other.example.com"
    with pytest.raises(ValueError, match="DOMAIN"):
        Settings(_env_file=None, telegram_bot_token="t", encryption_key=key)


def test_openssl_style_key_is_accepted():
    # What deploy/install.sh generates: `openssl rand -base64 32 | tr '+/' '-_'`.
    import base64
    import os

    key = base64.b64encode(os.urandom(32)).decode().replace("+", "-").replace("/", "_")
    settings = Settings(
        _env_file=None, telegram_bot_token="t", public_base_url="https://x", encryption_key=key
    )
    assert Crypto(settings.encryption_key).decrypt_json(Crypto(key).encrypt_json({"ok": True})) == {
        "ok": True
    }


def test_settings_reject_invalid_encryption_key():
    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        Settings(
            _env_file=None, telegram_bot_token="t", public_base_url="https://x", encryption_key="x"
        )
