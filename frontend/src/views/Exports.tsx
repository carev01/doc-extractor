import { useState, useEffect } from "react";
import type { ExportListItem } from "../types";
import {
  listExports,
  deleteExport,
  downloadExportZip,
  downloadExportFile,
} from "../api/client";

/** Standalone view (opened from the hamburger menu) listing recent generated
 * exports across all sources, with authenticated downloads and delete. */
export function Exports() {
  const [exports, setExports] = useState<ExportListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = () =>
    listExports()
      .then((res) => setExports(res.exports))
      .catch(() => setError("Failed to load exports"))
      .finally(() => setLoading(false));

  useEffect(() => {
    refresh();
  }, []);

  async function handleDelete(exportId: string) {
    if (!confirm("Delete this export? The generated files will be removed from the server.")) return;
    try {
      await deleteExport(exportId);
      await refresh();
    } catch {
      setError("Failed to delete export");
    }
  }

  async function download(fn: () => Promise<void>) {
    try {
      await fn();
    } catch {
      setError("Download failed — please try again.");
    }
  }

  return (
    <div className="exports-view">
      <h2>Exports</h2>
      <p className="sub">
        Generated exports are kept on the server for a limited time, then
        automatically removed.
      </p>
      {error && <p className="error">{error}</p>}
      {loading ? (
        <p className="hint">Loading…</p>
      ) : exports.length === 0 ? (
        <p className="hint">
          No exports yet. Generate one from a source's Export tab.
        </p>
      ) : (
        <ul className="recent-exports">
          {exports.map((ex) => (
            <li key={ex.export_id}>
              <div>
                <strong>{ex.source_name}</strong>{" "}
                <span className="sub">
                  · {ex.format.toUpperCase()} · {ex.file_count} file(s) ·{" "}
                  {(ex.total_size_bytes / 1024).toFixed(0)} KB ·{" "}
                  {new Date(ex.created_at).toLocaleString()}
                  {ex.expires_at && (
                    <> · expires {new Date(ex.expires_at).toLocaleDateString()}</>
                  )}
                </span>
              </div>
              <div className="export-links">
                {ex.zip_filename ? (
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => download(() => downloadExportZip(ex.export_id))}
                  >
                    Download ZIP
                  </button>
                ) : (
                  ex.files.map((f) => (
                    <button
                      key={f}
                      type="button"
                      className="link-btn"
                      onClick={() => download(() => downloadExportFile(ex.export_id, f))}
                    >
                      {f}
                    </button>
                  ))
                )}
                <button
                  type="button"
                  className="btn-danger-sm"
                  title="Delete this export from the server"
                  onClick={() => handleDelete(ex.export_id)}
                >
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
