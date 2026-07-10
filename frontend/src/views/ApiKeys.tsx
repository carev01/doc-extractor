import { useEffect, useState } from "react";
import { keysApi, accountApi } from "../api/client";
import { apiError } from "../api/errors";
import type { ApiKeyItem, ApiKeyCreated, AuthUser, AdminApiKey } from "../types";

/** Self-service: manage your own API keys. Admins also see all keys. */
export function ApiKeys({ me }: { me: AuthUser | null }) {
  const [keys, setKeys] = useState<ApiKeyItem[]>([]);
  const [name, setName] = useState("");
  const [role, setRole] = useState<"read_only" | "read_write" | "admin">("read_only");
  const [newKey, setNewKey] = useState<ApiKeyCreated | null>(null);
  const [err, setErr] = useState("");

  const roleOptions = (): typeof role[] => {
    if (me?.role === "admin") return ["read_only", "read_write", "admin"];
    if (me?.role === "read_write") return ["read_only", "read_write"];
    return ["read_only"];
  };

  const refresh = () => keysApi.listMine().then(setKeys).catch(() => {});
  useEffect(() => { refresh(); }, []);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr("");
    try {
      const created = await keysApi.create({ name, role });
      setNewKey(created);
      setName("");
      refresh();
    } catch (e2) {
      setErr(apiError(e2, "Failed to create key"));
    }
  };
  const rotate = async (id: string) => {
    if (!confirm("Rotate this key? The old value stops working immediately.")) return;
    try {
      setNewKey(await keysApi.rotate(id));
      refresh();
    } catch (e2) { setErr(apiError(e2, "Rotate failed")); }
  };
  const revoke = async (id: string) => {
    if (!confirm("Revoke this key?")) return;
    try { await keysApi.revoke(id); refresh(); }
    catch (e2) { setErr(apiError(e2, "Revoke failed")); }
  };

  return (
    <div className="apikeys-view">
      <h2>API Keys</h2>
      <p className="sub">
        Keys authenticate API clients via the <code>X-API-Key</code> header. A key's
        role can't exceed your own; the raw value is shown only once.
      </p>

      {newKey && (
        <div className="banner" style={{ padding: "0.8em", border: "1px solid #58c08a", borderRadius: 6, margin: "0.6em 0" }}>
          <strong>New key "{newKey.name}"</strong> — copy it now, it won't be shown again:
          <pre style={{ userSelect: "all", overflowX: "auto" }}>{newKey.raw_key}</pre>
          <button onClick={() => setNewKey(null)}>Dismiss</button>
        </div>
      )}

      <form onSubmit={create} className="add-form" style={{ display: "flex", gap: "0.5em", flexWrap: "wrap" }}>
        <input placeholder="Key name" value={name} onChange={(e) => setName(e.target.value)} required />
        <select value={role} onChange={(e) => setRole(e.target.value as typeof role)}>
          {roleOptions().map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <button type="submit">Create key</button>
        {err && <span className="error">{err}</span>}
      </form>

      <table style={{ width: "100%", fontSize: "0.9em", borderCollapse: "collapse", marginTop: "1em" }}>
        <thead>
          <tr style={{ textAlign: "left", color: "#888" }}>
            <th>Name</th><th>Prefix</th><th>Role</th><th>Status</th><th>Last used</th><th></th>
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.id} style={{ opacity: k.is_active ? 1 : 0.5 }}>
              <td>{k.name}</td>
              <td><code>{k.key_prefix}…</code></td>
              <td>{k.role}</td>
              <td>{k.is_active ? "active" : "revoked"}</td>
              <td>{k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "never"}</td>
              <td>
                {k.is_active && (
                  <>
                    <button className="btn-secondary-sm" onClick={() => rotate(k.id)}>Rotate</button>{" "}
                    <button className="btn-danger-sm" onClick={() => revoke(k.id)}>Revoke</button>
                  </>
                )}
              </td>
            </tr>
          ))}
          {keys.length === 0 && <tr><td colSpan={6} className="sub">No API keys yet.</td></tr>}
        </tbody>
      </table>

      {me?.role === "admin" && <AdminKeys />}
    </div>
  );
}

function AdminKeys() {
  const [keys, setKeys] = useState<AdminApiKey[]>([]);
  const refresh = () => keysApi.listAll().then(setKeys).catch(() => {});
  useEffect(() => { refresh(); }, []);
  const revoke = async (id: string) => {
    if (!confirm("Revoke this key?")) return;
    try { await keysApi.revoke(id); refresh(); } catch { /* ignore */ }
  };
  return (
    <div style={{ marginTop: "2em", borderTop: "1px solid var(--line)", paddingTop: "1.5em" }}>
      <h3>All API Keys</h3>
      <table style={{ width: "100%", fontSize: "0.9em", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ textAlign: "left", color: "#888" }}>
            <th>Owner</th><th>Name</th><th>Prefix</th><th>Role</th><th>Status</th><th>Last used</th><th></th>
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.id} style={{ opacity: k.is_active ? 1 : 0.5 }}>
              <td>{k.user_email}</td>
              <td>{k.name}</td>
              <td><code>{k.key_prefix}…</code></td>
              <td>{k.role}</td>
              <td>{k.is_active ? "active" : "revoked"}</td>
              <td>{k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "never"}</td>
              <td>{k.is_active && <button className="btn-danger-sm" onClick={() => revoke(k.id)}>Revoke</button>}</td>
            </tr>
          ))}
          {keys.length === 0 && <tr><td colSpan={7} className="sub">No keys.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

export function ChangePassword() {
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