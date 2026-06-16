import { useState, useEffect } from "react";
import type { Vendor, DocumentationSource } from "../types";
import {
  listSources,
  createSource,
  deleteSource,
  triggerExtraction,
} from "../api/client";

interface Props {
  vendor: Vendor;
  onSelectSource: (source: DocumentationSource) => void;
  selectedSourceId?: string;
}

export default function SourceList({
  vendor,
  onSelectSource,
  selectedSourceId,
}: Props) {
  const [sources, setSources] = useState<DocumentationSource[]>([]);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [extracting, setExtracting] = useState<string | null>(null);
  const [error, setError] = useState("");

  const fetchSources = async () => {
    try {
      const data = await listSources(vendor.id);
      setSources(data.sources);
    } catch (e) {
      setError("Failed to load sources");
    }
  };

  useEffect(() => {
    fetchSources();
  }, [vendor.id]);

  // Poll for status updates when extracting
  useEffect(() => {
    if (!extracting) return;
    const interval = setInterval(fetchSources, 2000);
    return () => clearInterval(interval);
  }, [extracting]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !baseUrl.trim()) return;
    setLoading(true);
    setError("");
    try {
      await createSource({
        vendor_id: vendor.id,
        name: name.trim(),
        base_url: baseUrl.trim(),
      });
      setName("");
      setBaseUrl("");
      await fetchSources();
    } catch (e: any) {
      setError(e.response?.data?.detail || "Failed to create source");
    } finally {
      setLoading(false);
    }
  };

  const handleExtract = async (sourceId: string) => {
    setExtracting(sourceId);
    setError("");
    try {
      await triggerExtraction(sourceId);
    } catch (e: any) {
      setError(e.response?.data?.detail || "Failed to trigger extraction");
      setExtracting(null);
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this source and all extracted articles?")) return;
    try {
      await deleteSource(id);
      await fetchSources();
    } catch (e: any) {
      setError(e.response?.data?.detail || "Failed to delete source");
    }
  };

  const statusBadge = (status: string) => {
    const colors: Record<string, string> = {
      pending: "#888",
      extracting: "#f0a030",
      completed: "#30a030",
      failed: "#d03030",
    };
    return (
      <span
        className="status-badge"
        style={{ backgroundColor: colors[status] || "#888" }}
      >
        {status}
      </span>
    );
  };

  return (
    <div className="source-list">
      <h2>Documentation Sources — {vendor.name}</h2>

      {error && <div className="error">{error}</div>}

      <form onSubmit={handleCreate} className="add-form">
        <input
          type="text"
          placeholder="Source name (e.g. 'API Docs')"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
        <input
          type="url"
          placeholder="Documentation base URL"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          required
        />
        <button type="submit" disabled={loading}>
          {loading ? "Adding..." : "Add Source"}
        </button>
      </form>

      <ul className="item-list">
        {sources.map((s) => (
          <li
            key={s.id}
            className={s.id === selectedSourceId ? "selected" : ""}
            onClick={() => onSelectSource(s)}
          >
            <div className="item-info">
              <strong>{s.name}</strong>
              <span className="sub">{s.base_url}</span>
              <div className="item-meta">
                {statusBadge(s.status)}
                {s.last_extracted_at && (
                  <span className="sub">
                    Last: {new Date(s.last_extracted_at).toLocaleString()}
                  </span>
                )}
              </div>
            </div>
            <div className="item-actions">
              <button
                className="btn-primary-sm"
                onClick={(e) => {
                  e.stopPropagation();
                  handleExtract(s.id);
                }}
                disabled={
                  extracting === s.id || s.status === "extracting"
                }
              >
                {extracting === s.id || s.status === "extracting"
                  ? "Extracting..."
                  : "Extract"}
              </button>
              <button
                className="btn-danger-sm"
                onClick={(e) => {
                  e.stopPropagation();
                  handleDelete(s.id);
                }}
              >
                ×
              </button>
            </div>
          </li>
        ))}
        {sources.length === 0 && (
          <li className="empty">
            No documentation sources yet. Add one above.
          </li>
        )}
      </ul>
    </div>
  );
}
