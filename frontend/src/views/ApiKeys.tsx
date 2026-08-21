import { useEffect, useState } from "react";
import { keysApi } from "../api/client";
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
    <div className="apikeys-view console-view">
      <h2>API Keys</h2>
      <p className="sub">
        Keys authenticate API clients via the <code>X-API-Key</code> header. A key's
        role can't exceed your own; the raw value is shown only once.
      </p>

      {newKey && (
        <div className="reveal-banner">
          <strong>New key "{newKey.name}"</strong> — copy it now, it won't be shown again:
          <pre>{newKey.raw_key}</pre>
          <button className="btn-secondary-sm" onClick={() => setNewKey(null)}>Dismiss</button>
        </div>
      )}

      <form onSubmit={create} className="add-form">
        <input placeholder="Key name" value={name} onChange={(e) => setName(e.target.value)} required />
        <select value={role} onChange={(e) => setRole(e.target.value as typeof role)}>
          {roleOptions().map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <button type="submit" className="btn-primary">Create key</button>
        {err && <span className="error">{err}</span>}
      </form>

      <table className="data-table">
        <thead>
          <tr>
            <th>Name</th><th>Prefix</th><th>Role</th><th>Status</th><th>Last used</th><th></th>
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.id} className={k.is_active ? "" : "is-inactive"}>
              <td>{k.name}</td>
              <td className="mono-cell">{k.key_prefix}…</td>
              <td>{k.role}</td>
              <td>{k.is_active ? "active" : "revoked"}</td>
              <td>{k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "never"}</td>
              <td>
                {k.is_active && (
                  <div className="cell-actions">
                    <button className="btn-secondary-sm" onClick={() => rotate(k.id)}>Rotate</button>
                    <button className="btn-danger-sm" onClick={() => revoke(k.id)}>Revoke</button>
                  </div>
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
    <div className="section-divider">
      <h3>All API Keys</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>Owner</th><th>Name</th><th>Prefix</th><th>Role</th><th>Status</th><th>Last used</th><th></th>
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k.id} className={k.is_active ? "" : "is-inactive"}>
              <td className="mono-cell">{k.user_email}</td>
              <td>{k.name}</td>
              <td className="mono-cell">{k.key_prefix}…</td>
              <td>{k.role}</td>
              <td>{k.is_active ? "active" : "revoked"}</td>
              <td>{k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "never"}</td>
              <td>
                {k.is_active && (
                  <div className="cell-actions">
                    <button className="btn-danger-sm" onClick={() => revoke(k.id)}>Revoke</button>
                  </div>
                )}
              </td>
            </tr>
          ))}
          {keys.length === 0 && <tr><td colSpan={7} className="sub">No keys.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
