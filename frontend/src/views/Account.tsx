import { useState } from "react";
import { accountApi } from "../api/client";
import { apiError } from "../api/errors";

/** Account settings page — password management. */
export function Account() {
  return (
    <div className="account-view">
      <h2>Account Settings</h2>
      <p className="sub">Update your password</p>
      <div className="account-card">
        <ChangePassword />
      </div>
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
    <>
      <h3>Change password</h3>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: "0.5em" }}>
        <input type="password" placeholder="Current password" value={cur} autoComplete="current-password" onChange={(e) => setCur(e.target.value)} required />
        <input type="password" placeholder="New password (min 8 chars)" value={next} autoComplete="new-password" minLength={8} onChange={(e) => setNext(e.target.value)} required />
        <button type="submit">Update password</button>
        {msg && <div className={msg.ok ? "sub" : "error"}>{msg.text}</div>}
      </form>
    </>
  );
}