import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.schemas.delta import encode_delta_cursor, decode_delta_cursor


def test_roundtrip():
    token = encode_delta_cursor(4811)
    assert decode_delta_cursor(token) == 4811


def test_zero():
    assert decode_delta_cursor(encode_delta_cursor(0)) == 0


def test_malformed_raises():
    with pytest.raises(ValueError):
        decode_delta_cursor("not-a-real-cursor!!")


def test_wrong_version_raises():
    from app.schemas.search import encode_cursor
    bad = encode_cursor({"v": 999, "seq": 5})
    with pytest.raises(ValueError):
        decode_delta_cursor(bad)
