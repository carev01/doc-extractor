import { useState } from "react";
import type { Product, DocumentationSource } from "../types";
import { bumpProductVersion } from "../api/client";

interface Props {
  product: Product & { version: string | null };
  sources: DocumentationSource[];
  onClose: () => void;
  onBumped: (newVersion: string) => void;
}

export default function BumpVersionModal({ product, sources, onClose, onBumped }: Props) {
  const [newVersion, setNewVersion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const templated = sources.filter((s) => s.url_template && s.url_template.includes("{version}"));
  const v = newVersion.trim();
  const canSubmit = v !== "" && v !== product.version && templated.length > 0;

  const preview = (s: DocumentationSource) =>
    (s.url_template as string).replaceAll("{version}", v || "{version}");

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError("");
    try {
      const res = await bumpProductVersion(product.id, v);
      onBumped(res.version);
    } catch {
      setError("Bump failed");
      setBusy(false);
    }
  };

  return (
    <div className="overlay-backdrop" onClick={onClose}>
      <div className="overlay-panel" onClick={(e) => e.stopPropagation()}>
        <h3>Bump {product.name} from {product.version} → {v || "?"}</h3>
        <input
          autoFocus
          placeholder="New version (e.g. 11.0)"
          value={newVersion}
          onChange={(e) => setNewVersion(e.target.value)}
        />
        <p className="muted">{templated.length} templated source(s) will be rewritten and re-extracted:</p>
        <ul className="bump-preview">
          {templated.map((s) => (
            <li key={s.id}>
              <code>{s.base_url}</code> → <code>{preview(s)}</code>
            </li>
          ))}
        </ul>
        {sources.length > templated.length && (
          <p className="muted">{sources.length - templated.length} non-templated source(s) unaffected.</p>
        )}
        {error && <span className="error-inline">{error}</span>}
        <div className="overlay-actions">
          <button type="button" disabled={!canSubmit || busy} onClick={submit}>
            {busy ? "Bumping…" : "Bump & re-extract"}
          </button>
          <button type="button" className="btn-secondary-sm" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
