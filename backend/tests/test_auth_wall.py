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


# AvePoint Learn, answering a request that carries an *expired* session cookie:
# 200, 404 bytes, and a meta refresh to its SSO endpoint. No cookie at all gets
# the public page, so neither the status nor the request URL gives it away.
AVEPOINT_SSO_SHELL = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Redirecting…</title></head>
<body data-redirect-url="/api/auth/aos-sso/COMMERCIAL?returnUrl=%2Ftygraph%2Fabout-avepoint-tygraph.html">
<script src="/js/fragment-redirect.js"></script>
<noscript><meta http-equiv="refresh" content="0;url=/api/auth/aos-sso/COMMERCIAL?returnUrl=%2Ftygraph%2Fabout-avepoint-tygraph.html"></noscript>
</body>
</html>"""


def test_redirect_shell_to_an_sso_endpoint_is_an_auth_wall():
    assert is_auth_wall(AVEPOINT_SSO_SHELL,
                        final_url="https://learn.avepoint.com/tygraph/about-avepoint-tygraph.html")


def test_meta_refresh_to_an_idp_host_is_an_auth_wall():
    html = '<html><head><meta http-equiv="refresh" content="0; URL=\'https://login.example.com/start\'"></head></html>'
    assert is_auth_wall(html)


def test_redirect_shell_to_an_ordinary_page_is_not_an_auth_wall():
    html = '<html><head><meta http-equiv="refresh" content="0;url=/docs/new-location.html"></head></html>'
    assert not is_auth_wall(html)


def test_long_page_mentioning_an_sso_redirect_is_not_an_auth_wall():
    # A real article that documents configuring SSO (and shows a refresh tag in
    # a code sample) is not a wall: the shell rule is short-page only.
    body = "<p>" + "Configure single sign-on for your tenant. " * 100 + "</p>"
    html = f'<html><body>{body}<pre>&lt;meta http-equiv="refresh" content="0;url=/api/auth/sso"&gt;</pre>' \
           f'<div data-redirect-url="/api/auth/aos-sso/x"></div></body></html>'
    assert not is_auth_wall(html)
