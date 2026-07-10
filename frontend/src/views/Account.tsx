import { useState } from "react";
import { accountApi } from "../api/client";
import { apiError } from "../api/errors";
import type { AuthUser } from "../types";

/** Account settings page — password management. */
export function Account({ me }: { me: AuthUser | null }) {
  return (
    <div className="account-view account-page">
      <h2>Account Settings</h2>
      {me?.oauth_provider == null ? (
        <ChangePassword />
      ) : (
        <div className="account-card">
          <p className="sub">
            You sign in via {me.oauth_provider}. Your password is managed by your
            identity provider and can't be changed here.
          </p>
        </div>
      )}
    </div>
  );
}

function ChangePassword() {
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setMsg(null);
    try {
      await accountApi.changePassword(cur, next);
      setMsg({ ok: true, text: "Password changed." });
      setCur(""); setNext("");
    } catch (e2) {
      setMsg({ ok: false, text: apiError(e2, "Change failed") });
    }
  };

  return (
    <div className="account-card">
      <form onSubmit={submit} className="account-form">
        <div className="account-form-header">
          <h3>Change Password</h3>
          <p className="sub">Update your account password.</p>
        </div>
        <div className="account-form-fields">
          <input type="password" placeholder="Current password" value={cur} autoComplete="current-password" onChange={(e) => setCur(e.target.value)} required />
          <input type="password" placeholder="New password (min 8 chars)" value={next} autoComplete="new-password" minLength={8} onChange={(e) => setNext(e.target.value)} required />
        </div>
        <button type="submit" className="btn-primary account-form-submit">Update password</button>
        {msg && <div className={msg.ok ? "sub account-form-msg" : "error account-form-msg"}>{msg.text}</div>}
      </form>
    </div>
  );
}
