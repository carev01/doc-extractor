import os
import sys

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core import crypto
from app.core.config import settings


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    crypto._reset_cache()  # clear the memoised cipher between tests
    yield
    crypto._reset_cache()


def test_encrypt_decrypt_roundtrip():
    assert crypto.decrypt(crypto.encrypt("hunter2")) == "hunter2"


def test_ciphertext_is_not_plaintext():
    assert "hunter2" not in crypto.encrypt("hunter2")


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(settings, "secret_key", "")
    crypto._reset_cache()
    with pytest.raises(crypto.MissingKeyError):
        crypto.encrypt("x")
