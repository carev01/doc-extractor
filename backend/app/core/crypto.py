"""Symmetric encryption for secrets stored at rest (Fernet).

The key comes from ``settings.secret_key`` (DOCEXTRACTOR_SECRET_KEY). The cipher
is built lazily and memoised so importing this module never fails when no key is
configured; only actually encrypting/decrypting requires the key.
"""

import json
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.config import settings

_cipher: Fernet | None = None


class MissingKeyError(RuntimeError):
    """Raised when encryption is attempted without DOCEXTRACTOR_SECRET_KEY set."""


def _reset_cache() -> None:
    """Test hook — drop the memoised cipher so a new key takes effect."""
    global _cipher
    _cipher = None


def _get_cipher() -> Fernet:
    global _cipher
    if _cipher is None:
        if not settings.secret_key:
            raise MissingKeyError(
                "DOCEXTRACTOR_SECRET_KEY is required to encrypt/decrypt secrets"
            )
        _cipher = Fernet(settings.secret_key.encode())
    return _cipher


def encrypt(plaintext: str) -> str:
    return _get_cipher().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _get_cipher().decrypt(token.encode()).decode()


class EncryptedStr(TypeDecorator):
    """A string column transparently encrypted at rest."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        return None if value is None else encrypt(value)

    def process_result_value(self, value: str | None, dialect) -> str | None:
        return None if value is None else decrypt(value)


class EncryptedJSON(TypeDecorator):
    """A JSON-serialisable column transparently encrypted at rest."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any | None, dialect) -> str | None:
        return None if value is None else encrypt(json.dumps(value))

    def process_result_value(self, value: str | None, dialect) -> Any | None:
        return None if value is None else json.loads(decrypt(value))
