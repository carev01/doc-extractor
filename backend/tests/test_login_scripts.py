import os
import sys

import pyotp
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import AuthRealm, RealmStatus
from app.services.auth import login_scripts


def _realm(**kw):
    base = dict(name="X", login_domain="docs.x.com", auth_type="form",
                login_url="https://x.com/login", username="u", password="p",
                browserless_profile_name="realm-x", status=RealmStatus.NEEDS_LOGIN)
    base.update(kw)
    return AuthRealm(**base)


def test_build_login_form_includes_creds_and_profile():
    code, ctx = login_scripts.build_login(_realm())
    assert "saveProfile" not in code
    assert "page.cookies()" in code
    assert ctx["username"] == "u" and ctx["password"] == "p"
    assert ctx["profileName"] == "realm-x"
    assert ctx["loginUrl"] == "https://x.com/login"


def test_build_login_computes_totp_when_seeded():
    secret = pyotp.random_base32()
    code, ctx = login_scripts.build_login(_realm(totp_secret=secret))
    assert ctx["otp"] == pyotp.TOTP(secret).now()


def test_build_login_selector_overrides_win():
    code, ctx = login_scripts.build_login(_realm(login_selectors={"username": "#email"}))
    assert ctx["selectors"]["username"] == "#email"


def test_login_code_presses_enter_when_no_submit_button():
    # Some login forms (e.g. AvePoint AOS SSO) have no clickable submit button
    # and submit on Enter. The template must fall back to pressing Enter when
    # none of the submit selectors match, instead of silently not submitting.
    code, _ = login_scripts.build_login(_realm())
    assert "keyboard.press('Enter')" in code
