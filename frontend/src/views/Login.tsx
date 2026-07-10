import { useState } from "react";
import { authApi } from "../api/client";
import { apiError } from "../api/errors";

/** Login screen shown when the backend has authentication enabled and the user
 *  has no valid session. On success it stores tokens and calls onSuccess. */
export function Login({
  needsBootstrap,
  onSuccess,
}: {
  needsBootstrap: boolean;
  onSuccess: () => void;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await authApi.login(email, password);
      onSuccess();
    } catch (err) {
      setError(apiError(err, "Login failed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ maxWidth: 360, margin: "10vh auto", padding: "1.5em" }}>
      <h2>DocExtractor</h2>
      {needsBootstrap ? (
        <p className="sub">
          No users exist yet. The first account you create becomes the
          administrator — register it via <code>POST /api/auth/register</code>,
          then sign in here.
        </p>
      ) : (
        <p className="sub">Sign in to continue.</p>
      )}
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: "0.6em" }}>
        <input
          type="email"
          placeholder="Email"
          value={email}
          autoComplete="username"
          onChange={(e) => setEmail(e.target.value)}
          required
        />
        <input
          type="password"
          placeholder="Password"
          value={password}
          autoComplete="current-password"
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        {error && <div className="error">{error}</div>}
      </form>
    </div>
  );
}
