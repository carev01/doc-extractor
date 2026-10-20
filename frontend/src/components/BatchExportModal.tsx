import { useCallback, useEffect, useState } from "react";
import type { ExportBatch, VendorExportProduct } from "../types";
import {
  createProductExport,
  createVendorExport,
  downloadExportBatch,
  getExportBatch,
  previewProductExport,
  previewVendorExport,
} from "../api/client";

/** What the batch covers: every source of one product, or of the chosen
 *  products of a vendor. Both fan out to one ordinary export job per source. */
export type BatchScope =
  | { kind: "product"; id: string; name: string }
  | { kind: "vendor"; id: string; name: string };

interface Props {
  scope: BatchScope;
  onClose: () => void;
}

/** The numbers the dialog shows, whichever scope produced them. */
interface Projection {
  sources: number;
  articles: number;
  markdownBytes: number;
  imageBytes: number;
  skipped: string[];
}

const STATUS_CLASS: Record<string, string> = {
  completed: "is-ok",
  running: "is-warn",
  pending: "is-muted",
  failed: "is-bad",
  cancelled: "is-muted",
};

// Same units as the single-source ExportPanel: articles, MB, plain tokens.
const SPLIT_DEFAULTS = { articles: 50, size: 10, tokens: 100_000 } as const;
const SPLIT_PLACEHOLDER = {
  articles: "Articles per file",
  size: "MB per file",
  tokens: "Tokens per file",
} as const;

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

export default function BatchExportModal({ scope, onClose }: Props) {
  const [format, setFormat] = useState<"markdown" | "pdf">("markdown");
  const [includeImages, setIncludeImages] = useState(false);
  const [splitBy, setSplitBy] = useState<"" | "size" | "articles" | "tokens">("");
  const [splitValue, setSplitValue] = useState(50);
  const [respectChapters, setRespectChapters] = useState(false);
  const [projection, setProjection] = useState<Projection | null>(null);
  const [products, setProducts] = useState<VendorExportProduct[] | null>(null);
  // Exclusions rather than a selection, so "everything" needs no initialisation
  // once the preview lands.
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [batch, setBatch] = useState<ExportBatch | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const what = scope.kind === "product" ? "product" : "vendor";

  // Project the size before committing the worker to it. Image payloads dwarf
  // the text, so this is the number worth seeing first.
  useEffect(() => {
    let cancelled = false;
    const load =
      scope.kind === "product"
        ? previewProductExport(scope.id, false).then((p) => {
            if (cancelled) return;
            setProjection({
              sources: p.exportable_source_count,
              articles: p.total_articles,
              markdownBytes: p.markdown_bytes,
              imageBytes: p.image_bytes,
              skipped: p.skipped,
            });
          })
        : previewVendorExport(scope.id).then((p) => {
            if (!cancelled) setProducts(p.products);
          });
    load.catch(() => {
      if (!cancelled) setError(`Could not read this ${what}'s sources`);
    });
    return () => {
      cancelled = true;
    };
  }, [scope.kind, scope.id, what]);

  // A vendor's totals follow the ticked products; derived, not stored.
  const chosen = (products ?? []).filter(
    (p) => p.exportable_source_count > 0 && !excluded.has(p.product_id),
  );
  const preview: Projection | null =
    scope.kind === "product"
      ? projection
      : products && {
          sources: chosen.reduce((n, p) => n + p.exportable_source_count, 0),
          articles: chosen.reduce((n, p) => n + p.total_articles, 0),
          markdownBytes: chosen.reduce((n, p) => n + p.markdown_bytes, 0),
          imageBytes: chosen.reduce((n, p) => n + p.image_bytes, 0),
          skipped: chosen.flatMap((p) => p.skipped.map((s) => `${p.name} / ${s}`)),
        };

  const toggle = (id: string) =>
    setExcluded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

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
      const options = {
        format,
        include_images: format === "pdf" ? false : includeImages,
        split_by: splitBy || null,
        max_articles_per_file: splitBy === "articles" ? splitValue : null,
        max_file_size_bytes: splitBy === "size" ? splitValue * 1024 * 1024 : null,
        // A plain token count, as in the single-source ExportPanel. This used to be
        // thousands ("By tokens (k)"): typing 500000 sent 500 million, so no
        // source ever split.
        max_tokens_per_file: splitBy === "tokens" ? splitValue : null,
        respect_chapters: respectChapters,
      };
      const created =
        scope.kind === "product"
          ? await createProductExport(scope.id, options)
          : await createVendorExport(scope.id, {
              ...options,
              product_ids: chosen.map((p) => p.product_id),
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
      ? includeImages
        ? humanBytes(preview.markdownBytes + preview.imageBytes)
        : `${humanBytes(preview.markdownBytes)} text only — images would add ${humanBytes(
            preview.imageBytes,
          )}`
      : null;

  return (
    <div className="picker-backdrop" onClick={onClose}>
      <div className="picker-panel is-wide" onClick={(e) => e.stopPropagation()}>
        <h3>
          {scope.kind === "product" ? "Export all sources" : "Export vendor"} — {scope.name}
        </h3>

        {!batch && (
          <>
            {scope.kind === "vendor" && products && (
              <ul className="bump-plan">
                {products.map((p) => {
                  const empty = p.exportable_source_count === 0;
                  return (
                    <li key={p.product_id} className="bump-plan-row">
                      <label className="chapter-toggle">
                        <input
                          type="checkbox"
                          disabled={empty}
                          checked={!empty && !excluded.has(p.product_id)}
                          onChange={() => toggle(p.product_id)}
                        />
                        <span className="bump-plan-name">{p.name}</span>
                        <span className="hint">
                          {" "}
                          —{" "}
                          {empty
                            ? "no articles yet"
                            : `${p.exportable_source_count} source(s), ${p.total_articles.toLocaleString()} articles, ${humanBytes(
                                p.markdown_bytes,
                              )}`}
                        </span>
                      </label>
                    </li>
                  );
                })}
              </ul>
            )}
            {preview ? (
              <p className="muted">
                {preview.sources} source(s), {preview.articles.toLocaleString()} articles
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
              <p className="muted">Reading this {what}'s sources…</p>
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
                  onChange={(e) => {
                    const next = e.target.value as "" | "size" | "articles" | "tokens";
                    setSplitBy(next);
                    // Each mode has its own scale; carrying 50 over from "articles"
                    // would mean 50-token files.
                    if (next) setSplitValue(SPLIT_DEFAULTS[next]);
                  }}
                >
                  <option value="">No split</option>
                  <option value="articles">By articles</option>
                  <option value="size">By size (MB)</option>
                  <option value="tokens">By tokens</option>
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
                    placeholder={SPLIT_PLACEHOLDER[splitBy]}
                    title={SPLIT_PLACEHOLDER[splitBy]}
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
                  <span className="hint"> — adds {humanBytes(preview.imageBytes)}</span>
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
                    <span className="bump-plan-name">
                      {scope.kind === "vendor" ? `${s.product_name} / ` : ""}
                      {s.source_name}
                    </span>
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
              disabled={busy || !preview || preview.sources === 0}
              onClick={start}
            >
              {busy
                ? "Starting…"
                : scope.kind === "product"
                  ? "Export all sources"
                  : `Export ${chosen.length} product(s)`}
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
