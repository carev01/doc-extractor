import { useCallback, useEffect, useState } from "react";
import type { ExportBatch, ProductExportPreview } from "../types";
import {
  createProductExport,
  downloadExportBatch,
  getExportBatch,
  previewProductExport,
} from "../api/client";

interface Props {
  productId: string;
  productName: string;
  onClose: () => void;
}

const STATUS_CLASS: Record<string, string> = {
  completed: "is-ok",
  running: "is-warn",
  pending: "is-muted",
  failed: "is-bad",
  cancelled: "is-muted",
};

function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["kB", "MB", "GB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

export default function ProductExportModal({ productId, productName, onClose }: Props) {
  const [format, setFormat] = useState<"markdown" | "pdf">("markdown");
  const [includeImages, setIncludeImages] = useState(false);
  const [splitBy, setSplitBy] = useState<"" | "size" | "articles" | "tokens">("");
  const [splitValue, setSplitValue] = useState(50);
  const [respectChapters, setRespectChapters] = useState(false);
  const [preview, setPreview] = useState<ProductExportPreview | null>(null);
  const [batch, setBatch] = useState<ExportBatch | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // Project the size before committing the worker to it. Image payloads dwarf
  // the text, so this is the number worth seeing first.
  useEffect(() => {
    let cancelled = false;
    previewProductExport(productId, includeImages)
      .then((p) => {
        if (!cancelled) setPreview(p);
      })
      .catch(() => {
        if (!cancelled) setError("Could not read this product's sources");
      });
    return () => {
      cancelled = true;
    };
  }, [productId, includeImages]);

  const refresh = useCallback(async (id: string) => {
    try {
      setBatch(await getExportBatch(id));
    } catch {
      /* transient; the next tick retries */
    }
  }, []);

  // Poll only while something is still queued or generating. Depends on the id
  // and the finished flag rather than the whole batch object, so a poll result
  // doesn't tear down and rebuild the interval on every tick.
  const batchId = batch?.batch_id;
  const batchFinished = batch?.finished ?? true;
  useEffect(() => {
    if (!batchId || batchFinished) return;
    const t = setInterval(() => void refresh(batchId), 1500);
    return () => clearInterval(t);
  }, [batchId, batchFinished, refresh]);

  const start = async () => {
    setBusy(true);
    setError("");
    try {
      const created = await createProductExport(productId, {
        format,
        include_images: format === "pdf" ? false : includeImages,
        split_by: splitBy || null,
        max_articles_per_file: splitBy === "articles" ? splitValue : null,
        max_file_size_bytes: splitBy === "size" ? splitValue * 1024 * 1024 : null,
        max_tokens_per_file: splitBy === "tokens" ? splitValue * 1000 : null,
        respect_chapters: respectChapters,
      });
      await refresh(created.batch_id);
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data
        ?.detail;
      setError(detail || "Could not start the export");
    } finally {
      setBusy(false);
    }
  };

  const sizeNote =
    preview && format === "markdown"
      ? `${humanBytes(preview.projected_bytes)}${
          includeImages ? "" : ` text only — images would add ${humanBytes(preview.image_bytes)}`
        }`
      : null;

  return (
    <div className="picker-backdrop" onClick={onClose}>
      <div className="picker-panel is-wide" onClick={(e) => e.stopPropagation()}>
        <h3>Export all sources — {productName}</h3>

        {!batch && (
          <>
            {preview ? (
              <p className="muted">
                {preview.exportable_source_count} source(s),{" "}
                {preview.total_articles.toLocaleString()} articles
                {sizeNote ? ` · ~${sizeNote}` : ""}
                {preview.skipped.length > 0 && (
                  <>
                    {" "}·{" "}
                    <span title={preview.skipped.join(", ")}>
                      {preview.skipped.length} source(s) skipped (no articles yet)
                    </span>
                  </>
                )}
              </p>
            ) : (
              <p className="muted">Reading this product's sources…</p>
            )}

            <div className="export-row">
              <label>
                Format
                <select
                  value={format}
                  onChange={(e) => setFormat(e.target.value as "markdown" | "pdf")}
                >
                  <option value="markdown">Markdown</option>
                  <option value="pdf">PDF</option>
                </select>
              </label>
              <label>
                Split
                <select
                  value={splitBy}
                  onChange={(e) =>
                    setSplitBy(e.target.value as "" | "size" | "articles" | "tokens")
                  }
                >
                  <option value="">No split</option>
                  <option value="articles">By articles</option>
                  <option value="size">By size (MB)</option>
                  <option value="tokens">By tokens (k)</option>
                </select>
              </label>
              {splitBy && (
                <label>
                  Limit
                  <input
                    type="number"
                    min={1}
                    value={splitValue}
                    onChange={(e) => setSplitValue(Number(e.target.value) || 1)}
                  />
                </label>
              )}
            </div>

            {format === "markdown" && (
              <label className="chapter-toggle">
                <input
                  type="checkbox"
                  checked={includeImages}
                  onChange={(e) => setIncludeImages(e.target.checked)}
                />
                Include images
                {preview && (
                  <span className="hint"> — adds {humanBytes(preview.image_bytes)}</span>
                )}
              </label>
            )}
            {splitBy && (
              <label className="chapter-toggle">
                <input
                  type="checkbox"
                  checked={respectChapters}
                  onChange={(e) => setRespectChapters(e.target.checked)}
                />
                Align file boundaries to chapters
              </label>
            )}
          </>
        )}

        {batch && (
          <>
            <p className="muted">
              {batch.completed} of {batch.total} done
              {batch.failed > 0 ? ` · ${batch.failed} failed` : ""}
              {batch.total_size_bytes > 0
                ? ` · ${humanBytes(batch.total_size_bytes)}`
                : ""}
            </p>
            <ul className="bump-plan">
              {batch.sources.map((s) => (
                <li key={s.job_id} className="bump-plan-row">
                  <div className="bump-plan-head">
                    <span className="bump-plan-name">{s.source_name}</span>
                    <span className={`status-badge ${STATUS_CLASS[s.status] ?? "is-muted"}`}>
                      {s.status}
                    </span>
                  </div>
                  {s.status === "completed" && (
                    <div className="bump-plan-note">
                      {s.article_count?.toLocaleString()} articles ·{" "}
                      {humanBytes(s.size_bytes ?? 0)}
                    </div>
                  )}
                  {s.error_message && (
                    <div className="bump-plan-note is-warn-text">{s.error_message}</div>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}

        {error && <span className="error-inline">{error}</span>}

        <div className="picker-actions">
          {!batch ? (
            <button
              type="button"
              className="btn-primary"
              disabled={busy || !preview || preview.exportable_source_count === 0}
              onClick={start}
            >
              {busy ? "Starting…" : "Export all sources"}
            </button>
          ) : (
            <button
              type="button"
              className="btn-primary"
              disabled={batch.completed === 0}
              title={
                batch.completed === 0
                  ? "Nothing has finished yet"
                  : batch.finished
                    ? undefined
                    : "Downloads what has finished so far"
              }
              onClick={() => void downloadExportBatch(batch.batch_id)}
            >
              Download zip
            </button>
          )}
          <button type="button" className="btn-secondary-sm" onClick={onClose}>
            {batch && !batch.finished ? "Close (keeps running)" : "Close"}
          </button>
        </div>
      </div>
    </div>
  );
}
