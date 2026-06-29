import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routes.auth_realms import _normalize_cookies


def test_cookie_editor_entry_normalized():
    out = _normalize_cookies([{
        "name": "SAML_TOKEN", "value": "tok", "domain": "docs.x.com", "path": "/",
        "secure": True, "httpOnly": True, "sameSite": "lax",
        "expirationDate": 1782679307.35, "session": False,
    }])
    assert out == [{
        "name": "SAML_TOKEN", "value": "tok", "domain": "docs.x.com", "path": "/",
        "secure": True, "httpOnly": True, "sameSite": "Lax", "expires": 1782679307.35,
    }]


def test_samesite_variants_and_session_cookie():
    out = _normalize_cookies([
        {"name": "a", "value": "1", "sameSite": "no_restriction", "secure": True},
        {"name": "b", "value": "2", "sameSite": None},               # session, sameSite omitted
        {"name": "c", "value": "3", "sameSite": "Strict", "expires": 123},  # already normalized
    ])
    assert out[0]["sameSite"] == "None"
    assert "sameSite" not in out[1] and "expires" not in out[1]
    assert out[2]["sameSite"] == "Strict" and out[2]["expires"] == 123


def test_missing_name_skipped_and_value_defaults():
    out = _normalize_cookies([{"value": "x"}, {"name": "ok"}])
    assert len(out) == 1
    assert out[0] == {"name": "ok", "value": "", "domain": "", "path": "/",
                      "secure": False, "httpOnly": False}
