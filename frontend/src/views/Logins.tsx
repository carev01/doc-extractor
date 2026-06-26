import { useEffect, useState } from 'react';
import { authRealmApi } from '../api/client';
import { apiError } from '../api/errors';
import type { AuthRealm, AuthRealmCreate } from '../types';

const STATUS_COLORS: Record<string, string> = {
  active: '#58c08a',
  needs_login: '#eaa53d',
  expired: '#6f8087',
  login_failed: '#e0685f',
};

function statusBadge(status: string) {
  return (
    <span
      className="status-badge"
      style={{ backgroundColor: STATUS_COLORS[status] || '#888' }}
    >
      {status.replace('_', ' ')}
    </span>
  );
}

const EMPTY: AuthRealmCreate = {
  name: '',
  login_domain: '',
  auth_type: 'form',
  login_url: '',
  username: '',
  password: '',
  totp_secret: '',
};

export function Logins() {
  const [realms, setRealms] = useState<AuthRealm[]>([]);
  const [form, setForm] = useState<AuthRealmCreate>(EMPTY);
  const [adding, setAdding] = useState(false);
  const [formError, setFormError] = useState('');
  const [actionError, setActionError] = useState<Record<string, string>>({});
  // Per-realm upload session state: id → textarea value
  const [sessionInput, setSessionInput] = useState<Record<string, string>>({});
  // Per-realm: whether upload session panel is open
  const [sessionOpen, setSessionOpen] = useState<Record<string, boolean>>({});

  const refresh = () => authRealmApi.list().then(setRealms).catch(() => {});
  useEffect(() => { refresh(); }, []);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError('');
    setAdding(true);
    try {
      await authRealmApi.create(form);
      setForm(EMPTY);
      refresh();
    } catch (err) {
      setFormError(apiError(err, 'Failed to create realm'));
    } finally {
      setAdding(false);
    }
  };

  const setRealmError = (id: string, msg: string) =>
    setActionError((prev) => ({ ...prev, [id]: msg }));
  const clearRealmError = (id: string) =>
    setActionError((prev) => { const next = { ...prev }; delete next[id]; return next; });

  const handleLogin = async (id: string) => {
    clearRealmError(id);
    try {
      await authRealmApi.login(id);
      refresh();
    } catch (err) {
      setRealmError(id, apiError(err, 'Login failed'));
    }
  };

  const handleTest = async (id: string) => {
    clearRealmError(id);
    try {
      await authRealmApi.test(id);
      refresh();
    } catch (err) {
      setRealmError(id, apiError(err, 'Test failed'));
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this auth realm?')) return;
    clearRealmError(id);
    try {
      await authRealmApi.remove(id);
      refresh();
    } catch (err) {
      setRealmError(id, apiError(err, 'Delete failed'));
    }
  };

  const handleUploadSession = async (id: string) => {
    clearRealmError(id);
    const raw = sessionInput[id] ?? '';
    let parsed: { cookies: unknown[]; origins: unknown[] };
    try {
      parsed = JSON.parse(raw);
    } catch {
      setRealmError(id, 'Invalid JSON — paste a valid Playwright storageState object');
      return;
    }
    if (!Array.isArray(parsed.cookies) || !Array.isArray(parsed.origins)) {
      setRealmError(id, 'JSON must have "cookies" and "origins" arrays');
      return;
    }
    try {
      await authRealmApi.uploadSession(id, parsed);
      setSessionInput((prev) => { const next = { ...prev }; delete next[id]; return next; });
      setSessionOpen((prev) => ({ ...prev, [id]: false }));
      refresh();
    } catch (err) {
      setRealmError(id, apiError(err, 'Upload session failed'));
    }
  };

  return (
    <div className="logins-view">
      <h2>Auth Realms</h2>

      <form onSubmit={handleCreate} className="add-form">
        <input
          placeholder="Name (e.g. Acme Docs)"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          required
        />
        <input
          placeholder="Login domain (e.g. docs.example.com)"
          value={form.login_domain}
          onChange={(e) => setForm({ ...form, login_domain: e.target.value })}
          required
        />
        <select
          value={form.auth_type}
          onChange={(e) =>
            setForm({ ...form, auth_type: e.target.value as AuthRealmCreate['auth_type'] })
          }
        >
          <option value="form">form</option>
          <option value="b2c">b2c</option>
          <option value="oidc">oidc</option>
        </select>
        <input
          placeholder="Login URL (optional)"
          value={form.login_url ?? ''}
          onChange={(e) => setForm({ ...form, login_url: e.target.value || null })}
        />
        <input
          placeholder="Username (optional)"
          value={form.username ?? ''}
          onChange={(e) => setForm({ ...form, username: e.target.value || null })}
        />
        <input
          type="password"
          placeholder="Password (optional)"
          value={form.password ?? ''}
          onChange={(e) => setForm({ ...form, password: e.target.value || null })}
        />
        <input
          placeholder="TOTP secret (optional)"
          value={form.totp_secret ?? ''}
          onChange={(e) => setForm({ ...form, totp_secret: e.target.value || null })}
        />
        <button type="submit" disabled={adding}>
          {adding ? 'Adding...' : 'Add realm'}
        </button>
        {formError && <div className="error">{formError}</div>}
      </form>

      <ul className="item-list">
        {realms.map((r) => (
          <li key={r.id} className="non-clickable">
            <div className="item-info">
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5em' }}>
                {statusBadge(r.status)}
                <strong>{r.name}</strong>
                <code className="sub">{r.login_domain}</code>
                <span className="sub">[{r.auth_type}]</span>
              </div>
              <div className="item-meta">
                {r.has_username && <span className="sub">has username</span>}
                {r.has_password && <span className="sub">· has password</span>}
                {r.has_totp && <span className="sub">· has TOTP</span>}
                {r.last_login_at && (
                  <span className="sub">
                    · last login: {new Date(r.last_login_at).toLocaleString()}
                  </span>
                )}
              </div>
              {r.error_message && (
                <div className="error" style={{ marginTop: '0.3em' }}>
                  {r.error_message}
                </div>
              )}
              {actionError[r.id] && (
                <div className="error" style={{ marginTop: '0.3em' }}>
                  {actionError[r.id]}
                </div>
              )}

              {sessionOpen[r.id] && (
                <div style={{ marginTop: '0.5em' }}>
                  <p className="sub" style={{ marginBottom: '0.3em' }}>
                    Log in in your own browser, export storage state (Playwright{' '}
                    <code>storageState</code>, or a cookie/localStorage export), and paste it here.
                  </p>
                  <textarea
                    rows={6}
                    style={{ width: '100%', fontFamily: 'monospace', fontSize: '0.8em' }}
                    placeholder={'{\n  "cookies": [],\n  "origins": []\n}'}
                    value={sessionInput[r.id] ?? ''}
                    onChange={(e) =>
                      setSessionInput((prev) => ({ ...prev, [r.id]: e.target.value }))
                    }
                  />
                  <div style={{ display: 'flex', gap: '0.4em', marginTop: '0.3em' }}>
                    <button
                      type="button"
                      className="btn-primary-sm"
                      onClick={() => handleUploadSession(r.id)}
                    >
                      Upload session
                    </button>
                    <button
                      type="button"
                      className="btn-secondary-sm"
                      onClick={() =>
                        setSessionOpen((prev) => ({ ...prev, [r.id]: false }))
                      }
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}
            </div>

            <div className="item-actions">
              {r.has_password && (
                <button
                  type="button"
                  className="btn-secondary-sm"
                  onClick={() => handleLogin(r.id)}
                >
                  Log in
                </button>
              )}
              <button
                type="button"
                className="btn-secondary-sm"
                onClick={() =>
                  setSessionOpen((prev) => ({ ...prev, [r.id]: !prev[r.id] }))
                }
              >
                Upload session
              </button>
              <button
                type="button"
                className="btn-secondary-sm"
                onClick={() => handleTest(r.id)}
              >
                Test
              </button>
              <button
                type="button"
                className="btn-danger-sm"
                onClick={() => handleDelete(r.id)}
              >
                ×
              </button>
            </div>
          </li>
        ))}
        {realms.length === 0 && (
          <li className="empty">No auth realms configured yet. Add one above.</li>
        )}
      </ul>
    </div>
  );
}
