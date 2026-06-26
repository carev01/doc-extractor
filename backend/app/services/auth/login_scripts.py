"""Per-auth-type Browserless /function login templates.

Each builder returns (esm_code, context). The ESM fills the login form, submits,
waits for navigation, then Browserless.saveProfile(profileName). Selectors have
per-auth-type defaults overridable via realm.login_selectors.
"""

import pyotp

from app.models.auth_realm import AuthRealm

_DEFAULT_SELECTORS = {
    "form": {"username": "input[type=email],input[name=username],#username",
             "password": "input[type=password]",
             "submit": "button[type=submit],input[type=submit]",
             "otp": "input[autocomplete=one-time-code],input[name=otp]"},
    "b2c": {"username": "#email,#signInName",
            "password": "#password",
            "submit": "#next,#continue,button[type=submit]",
            "otp": "#otpCode"},
    "oidc": {"username": "input[name=identifier],#username",
             "password": "input[type=password]",
             "submit": "button[type=submit]",
             "otp": "input[name=otp]"},
}

# Generic form login ESM. Types creds, optional OTP, submits, waits, saves.
_LOGIN_CODE = r"""
export default async function ({ page, context }) {
  const { loginUrl, username, password, otp, selectors, profileName, waitMs } = context;
  await page.goto(loginUrl, { waitUntil: 'networkidle2', timeout: 60000 });
  const type = async (sel, val) => {
    if (!val) return;
    const el = await page.waitForSelector(sel.split(',')[0], { timeout: 15000 }).catch(() => null)
            || await page.$(sel);
    if (el) { await el.click({ clickCount: 3 }).catch(() => {}); await el.type(val, { delay: 20 }); }
  };
  await type(selectors.username, username);
  // B2C/OIDC may need a "next" between email and password; click submit if password not yet visible.
  const pw = await page.$(selectors.password.split(',')[0]);
  if (!pw) { const n = await page.$(selectors.submit.split(',')[0]); if (n) await n.click().catch(() => {}); }
  await type(selectors.password, password);
  const submit = await page.$(selectors.submit.split(',')[0]);
  if (submit) await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 60000 }).catch(() => {}),
    submit.click(),
  ]);
  if (otp) { await type(selectors.otp, otp);
    const s2 = await page.$(selectors.submit.split(',')[0]);
    if (s2) await Promise.all([
      page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 60000 }).catch(() => {}),
      s2.click(),
    ]);
  }
  await new Promise(r => setTimeout(r, waitMs || 3000));
  const cookies = await page.cookies();
  const origin = new URL(page.url()).origin;
  const localStorage = await page.evaluate(() => Object.entries(window.localStorage));
  return { data: { ok: cookies.length > 0, cookieCount: cookies.length, finalUrl: page.url(),
                   state: { cookies, origins: [{ origin, localStorage }] } } };
}
"""


def build_login(realm: AuthRealm) -> tuple[str, dict]:
    defaults = _DEFAULT_SELECTORS.get(realm.auth_type, _DEFAULT_SELECTORS["form"])
    selectors = {**defaults, **(realm.login_selectors or {})}
    otp = pyotp.TOTP(realm.totp_secret).now() if realm.totp_secret else None
    context = {
        "loginUrl": realm.login_url,
        "username": realm.username,
        "password": realm.password,
        "otp": otp,
        "selectors": selectors,
        "profileName": realm.browserless_profile_name,
        "waitMs": 3000,
    }
    return _LOGIN_CODE, context
