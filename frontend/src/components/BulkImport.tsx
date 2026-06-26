import { useState } from "react";
import type { SourceImportResult } from "../types";
import { importSources } from "../api/client";

const SAMPLE = "vendor,product,source_name,base_url,url_template";

export default function BulkImport({
  onClose,
  onImported,
}: {
  onClose: () => void;
  onImported: () => void;
}) {
  const [csv, setCsv] = useState("");
  const [result, setResult] = useState<SourceImportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const onFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    file.text().then(setCsv);
  };

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const r = await importSources(csv);
      setResult(r);
      onImported();
    } catch (err: unknown) {
      const detail =
        typeof err === "object" && err !== null && "response" in err
          ? (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
          : undefined;
      setError(detail || "Import failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="picker-backdrop" onClick={onClose}>
      <div className="picker-panel" onClick={(e) => e.stopPropagation()}>
        <h3>Import sources (CSV)</h3>
        <p className="sub">Columns: {SAMPLE}</p>
        {error && <div className="error">{error}</div>}
        <input type="file" accept=".csv,text/csv" onChange={onFile} />
        <textarea
          rows={8}
          placeholder={SAMPLE}
          value={csv}
          onChange={(e) => setCsv(e.target.value)}
          style={{ width: "100%", marginTop: "0.5rem" }}
        />
        {result && (
          <div className="import-result">
            <p className="sub">
              Created {result.created} · Skipped {result.skipped} · Errors {result.errors}
            </p>
            <ul className="picker-list">
              {result.rows
                .filter((r) => r.result !== "created")
                .map((r) => (
                  <li key={r.row} className="sub">
                    Row {r.row}: {r.result} — {r.message}
                  </li>
                ))}
            </ul>
          </div>
        )}
        <div className="picker-actions">
          <button className="btn-secondary-sm" onClick={onClose}>Close</button>
          <button
            className="btn-primary-sm"
            disabled={busy || !csv.trim()}
            onClick={submit}
          >
            {busy ? "Importing…" : "Import"}
          </button>
        </div>
      </div>
    </div>
  );
}
