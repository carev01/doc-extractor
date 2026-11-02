import { useState, useEffect } from "react";
import type { ExportBatchSummary, ExportListItem } from "../types";
import {
  listExports,
  listExportBatches,
  deleteExport,
  downloadExportZip,
  downloadExportFile,
  downloadExportBatch,
} from "../api/client";

/** "12.3 MB" / "840 KB" — batch totals run far past the per-export KB scale. */
function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / 1024).toFixed(0)} KB`;
}

/** Standalone view (opened from the hamburger menu) listing recent generated
 * exports across all sources, with authenticated downloads and delete. */
export function Exports() {
  const [exports, setExports] = useState<ExportListItem[]>([]);
  const [batches, setBatches] = useState<ExportBatchSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = () =>
    Promise.all([listExports(), listExportBatches()])
      .then(([res, b]) => {
        setExports(res.exports);
        setBatches(b);
      })
      .catch(() => setError("Failed to load exports"))
      .finally(() => setLoading(false));

  useEffect(() => {
    refresh();
  }, []);

  // A batch still generating: keep its counts (and its per-source exports below)
  // moving without a manual reload.
  const inFlight = batches.some((b) => !b.finished);
  useEffect(() => {
    if (!inFlight) return;
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [inFlight]);

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
    <div className="exports-view console-view">
      <h2>Exports</h2>
      <p className="sub">
        Generated exports are kept on the server for a limited time, then
        automatically removed.
      </p>
      {error && <p className="error">{error}</p>}
      {!loading && batches.length > 0 && (
        <>
          <h3>Product &amp; vendor exports</h3>
          <ul className="item-list">
            {batches.map((b) => (
              <li key={b.batch_id} className="non-clickable">
                <div className="item-info">
                  <div className="card-head">
                    <span className="status-badge is-muted">
                      {b.product_count > 1 ? "vendor" : "product"}
                    </span>
                    <strong>{b.label}</strong>
                  </div>
                  <div className="item-meta">
                    <span className="sub">
                      {b.completed} of {b.total} source(s) ready
                    </span>
                    {!b.finished && (
                      <span className="sub">· {b.pending + b.running} in progress</span>
                    )}
                    {b.failed > 0 && <span className="sub">· {b.failed} failed</span>}
                    <span className="sub">· {formatSize(b.total_size_bytes)}</span>
                    <span className="sub">· {new Date(b.created_at).toLocaleString()}</span>
                    {b.expires_at && (
                      <span className="sub">
                        · expires {new Date(b.expires_at).toLocaleDateString()}
                      </span>
                    )}
                  </div>
                </div>
                <div className="export-links">
                  <button
                    type="button"
                    className="btn-secondary-sm"
                    disabled={!b.downloadable}
                    title={
                      b.downloadable
                        ? b.finished
                          ? "One zip with a folder per source"
                          : "Download what has completed so far"
                        : "No source has finished yet"
                    }
                    onClick={() => download(() => downloadExportBatch(b.batch_id))}
                  >
                    Download combined ZIP
                  </button>
                </div>
              </li>
            ))}
          </ul>
          <h3>Per-source exports</h3>
        </>
      )}
      {loading ? (
        <p className="hint">Loading…</p>
      ) : exports.length === 0 ? (
        <p className="hint">
          No exports yet. Generate one from a source's Export tab.
        </p>
      ) : (
        <ul className="item-list">
          {exports.map((ex) => (
            <li key={ex.export_id} className="non-clickable">
              <div className="item-info">
                <div className="card-head">
                  <span className="status-badge is-muted">{ex.format}</span>
                  <strong>{ex.source_name}</strong>
                </div>
                <div className="item-meta">
                  <span className="sub">{ex.file_count} file(s)</span>
                  <span className="sub">· {(ex.total_size_bytes / 1024).toFixed(0)} KB</span>
                  <span className="sub">· {new Date(ex.created_at).toLocaleString()}</span>
                  {ex.expires_at && (
                    <span className="sub">
                      · expires {new Date(ex.expires_at).toLocaleDateString()}
                    </span>
                  )}
                </div>
              </div>
              <div className="export-links">
                {ex.zip_filename ? (
                  <button
                    type="button"
                    className="btn-secondary-sm"
                    onClick={() => download(() => downloadExportZip(ex.export_id))}
                  >
                    Download ZIP
                  </button>
                ) : (
                  ex.files.map((f) => (
                    <button
                      key={f}
                      type="button"
                      className="btn-secondary-sm export-file"
                      title={`Download ${f}`}
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
