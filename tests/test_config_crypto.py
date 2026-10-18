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


def test_settings_reject_invalid_encryption_key():
    with pytest.raises(ValueError, match="ENCRYPTION_KEY"):
        Settings(
            _env_file=None, telegram_bot_token="t", public_base_url="https://x", encryption_key="x"
        )
