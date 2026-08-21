import { useEffect, useState } from "react";
import { usersApi, listAllVendors } from "../api/client";
import { apiError } from "../api/errors";
import type { AuthUser, Vendor } from "../types";

type Role = "admin" | "read_write" | "read_only";
type Level = "none" | "read_only" | "read_write";

/** Admin console: users + roles, per-vendor permissions, and API-key oversight. */
export function Admin({ meId }: { meId: string | null }) {
  const [users, setUsers] = useState<AuthUser[]>([]);
  const [err, setErr] = useState("");
  const [editingPerms, setEditingPerms] = useState<AuthUser | null>(null);

  const refresh = () => usersApi.list().then((d) => setUsers(d.users)).catch((e) => setErr(apiError(e, "Load failed")));
  useEffect(() => { refresh(); }, []);

  const setRole = async (u: AuthUser, role: Role) => {
    try { await usersApi.update(u.id, { role }); refresh(); }
    catch (e) { setErr(apiError(e, "Update failed")); }
  };
  const toggleActive = async (u: AuthUser) => {
    try { await usersApi.update(u.id, { is_active: !u.is_active }); refresh(); }
    catch (e) { setErr(apiError(e, "Update failed")); }
  };
  const del = async (u: AuthUser) => {
    if (!confirm(`Delete ${u.email}? This removes their keys and permissions.`)) return;
    try { await usersApi.remove(u.id); refresh(); }
    catch (e) { setErr(apiError(e, "Delete failed")); }
  };

  return (
    <div className="admin-view console-view">
      <h2>User Management</h2>
      {err && <div className="error">{err}</div>}

      <CreateUser onCreated={refresh} />

      <h3>Users</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>Email</th><th>Name</th><th>Role</th><th>Active</th><th>Sign-in</th><th></th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id} className={u.is_active ? "" : "is-inactive"}>
              <td className="mono-cell">{u.email}{u.id === meId && <span className="sub"> (you)</span>}</td>
              <td>{u.display_name}</td>
              <td>
                <select value={u.role} disabled={u.id === meId} onChange={(e) => setRole(u, e.target.value as Role)}>
                  <option value="read_only">read_only</option>
                  <option value="read_write">read_write</option>
                  <option value="admin">admin</option>
                </select>
              </td>
              <td>
                <button className="btn-secondary-sm" disabled={u.id === meId} onClick={() => toggleActive(u)}>
                  {u.is_active ? "Disable" : "Enable"}
                </button>
              </td>
              <td className="sub">{u.oauth_provider ?? "password"}</td>
              <td>
                <div className="cell-actions">
                  <button className="btn-secondary-sm" disabled={u.role === "admin"} title={u.role === "admin" ? "Admins see all vendors" : ""} onClick={() => setEditingPerms(u)}>
                    Vendor access
                  </button>
                  <button className="btn-danger-sm" disabled={u.id === meId} onClick={() => del(u)}>Delete</button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {editingPerms && (
        <VendorPermsEditor user={editingPerms} onClose={() => setEditingPerms(null)} />
      )}
    </div>
  );
}

function CreateUser({ onCreated }: { onCreated: () => void }) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("read_only");
  const [err, setErr] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr("");
    try {
      await usersApi.register({ email, display_name: name, password, role });
      setEmail(""); setName(""); setPassword(""); setRole("read_only");
      onCreated();
    } catch (e2) { setErr(apiError(e2, "Create failed")); }
  };

  return (
    <form onSubmit={submit} className="add-form">
      <input type="email" placeholder="Email" value={email} onChange={(e) => setEmail(e.target.value)} required />
      <input placeholder="Display name" value={name} onChange={(e) => setName(e.target.value)} required />
      <input type="password" placeholder="Temp password (min 8)" value={password} minLength={8} onChange={(e) => setPassword(e.target.value)} required />
      <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
        <option value="read_only">read_only</option>
        <option value="read_write">read_write</option>
        <option value="admin">admin</option>
      </select>
      <button type="submit" className="btn-primary">Add user</button>
      {err && <span className="error">{err}</span>}
    </form>
  );
}

function VendorPermsEditor({ user, onClose }: { user: AuthUser; onClose: () => void }) {
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [levels, setLevels] = useState<Record<string, Level>>({});
  const [err, setErr] = useState("");
  const [saved, setSaved] = useState(false);
  // A read_only user can't be granted read_write on any vendor.
  const rwAllowed = user.role !== "read_only";

  useEffect(() => {
    (async () => {
      try {
        const [vs, perms] = await Promise.all([
          listAllVendors(),
          usersApi.getVendorPerms(user.id),
        ]);
        setVendors(vs);
        const map: Record<string, Level> = {};
        for (const p of perms.permissions) map[p.vendor_id] = p.level as Level;
        setLevels(map);
      } catch (e) { setErr(apiError(e, "Load failed")); }
    })();
  }, [user.id]);

  const save = async () => {
    setErr(""); setSaved(false);
    const permissions = Object.entries(levels)
      .filter(([, lvl]) => lvl !== "none")
      .map(([vendor_id, level]) => ({ vendor_id, level }));
    try {
      await usersApi.setVendorPerms(user.id, permissions);
      setSaved(true);
    } catch (e) { setErr(apiError(e, "Save failed")); }
  };

  return (
    <div className="perms-panel">
      <div className="perms-head">
        <h3>Vendor access — {user.email} <span className="sub">(global role: {user.role})</span></h3>
        <button className="btn-secondary-sm" onClick={onClose}>Close</button>
      </div>
      <p className="sub perms-hint">
        Ungranted vendors are invisible to this user.
        {!rwAllowed && " Read-write is unavailable because their global role is read_only."}
      </p>
      <div className="perms-grid">
        {vendors.map((v) => {
          const lvl = levels[v.id] ?? "none";
          return (
            <label key={v.id} className={`perms-row${lvl === "none" ? "" : " is-granted"}`}>
              <span className="perms-vendor" title={v.name}>{v.name}</span>
              <select
                value={lvl}
                onChange={(e) => setLevels({ ...levels, [v.id]: e.target.value as Level })}
              >
                <option value="none">No access</option>
                <option value="read_only">Read-only</option>
                <option value="read_write" disabled={!rwAllowed}>Read-write</option>
              </select>
            </label>
          );
        })}
      </div>
      {/* Don't claim the list is empty when we simply failed to load it — that
          read as "this install has no vendors" while 40 existed. */}
      {vendors.length === 0 && (
        <p className="sub">
          {err ? "Could not load the vendor list — nothing to grant until it loads." : "No vendors exist yet."}
        </p>
      )}
      <div className="perms-foot">
        <button className="btn-primary-sm" onClick={save}>Save access</button>
        {saved && <span className="sub">Saved.</span>}
        {err && <span className="error">{err}</span>}
      </div>
    </div>
  );
}


