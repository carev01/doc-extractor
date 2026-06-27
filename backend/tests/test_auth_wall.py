import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.blockpage import is_auth_wall


def test_cohesity_wall_text_detected():
    assert is_auth_wall("Access to this product documentation requires authentication. Please sign in to continue.")


def test_idp_redirect_detected():
    assert is_auth_wall("redirecting...", final_url="https://onepassport.rubrik.com/oauth2/v1/authorize?x=1")


def test_real_doc_about_authentication_not_flagged():
    body = ("This guide explains how to configure single sign-on. " * 80)
    assert not is_auth_wall(body, final_url="https://docs.rubrik.com/en-us/sso.html")


def test_b2c_login_host_redirect_detected():
    assert is_auth_wall("", final_url="https://apwebapp.b2clogin.com/whatever")


def test_doc_url_with_login_in_path_not_flagged():
    # A legitimate doc page whose PATH contains "login" must not be misread as an
    # auth wall — the bare marker only counts in the hostname.
    body = ("How to configure a login policy for your environment. " * 80)
    assert not is_auth_wall(
        body, final_url="https://docs.cohesity.com/guides/configure-login-policy.html"
    )
