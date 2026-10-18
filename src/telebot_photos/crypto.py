"""Symmetric encryption for the credentials stored in the database."""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet


class Crypto:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt_json(self, data: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(data).encode())

    def decrypt_json(self, token: bytes) -> dict[str, Any]:
        return json.loads(self._fernet.decrypt(token))
