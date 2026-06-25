import { useState } from "react";
import type { Product, DocumentationSource } from "../types";
import { enableProductVersioning } from "../api/client";
import BumpVersionModal from "./BumpVersionModal";

interface Props {
  product: Product;
  sources: DocumentationSource[];
  onChanged: () => void;
}

export default function ProductVersionBar({ product, sources, onChanged }: Props) {
  const [version, setVersion] = useState<string | null>(product.version);
  const [enabling, setEnabling] = useState(false);
  const [enableValue, setEnableValue] = useState("");
  const [showBump, setShowBump] = useState(false);
  const [error, setError] = useState("");

  const submitEnable = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!enableValue.trim()) return;
    setError("");
    try {
      const res = await enableProductVersioning(product.id, enableValue.trim());
      setVersion(res.version);
      setEnabling(false);
      setEnableValue("");
      onChanged();
    } catch {
      setError("Failed to enable versioning");
    }
  };

  return (
    <div className="version-bar">
      {version !== null ? (
        <>
          <span className="version-badge">Version: {version}</span>
          <button type="button" className="btn-secondary-sm" onClick={() => setShowBump(true)}>
            Bump version
          </button>
        </>
      ) : enabling ? (
        <form onSubmit={submitEnable} className="version-enable-form">
          <input
            autoFocus
            placeholder="Current version (e.g. 10.0)"
            value={enableValue}
            onChange={(e) => setEnableValue(e.target.value)}
          />
          <button type="submit">Enable</button>
          <button type="button" className="btn-secondary-sm" onClick={() => setEnabling(false)}>
            Cancel
          </button>
        </form>
      ) : (
        <button type="button" className="btn-secondary-sm" onClick={() => setEnabling(true)}>
          Enable versioning
        </button>
      )}
      {error && <span className="error-inline">{error}</span>}
      {showBump && (
        <BumpVersionModal
          product={{ ...product, version }}
          sources={sources}
          onClose={() => setShowBump(false)}
          onBumped={(newVersion) => {
            setVersion(newVersion);
            setShowBump(false);
            onChanged();
          }}
        />
      )}
    </div>
  );
}
