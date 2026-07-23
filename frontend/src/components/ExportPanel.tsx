import { useState, useEffect, useRef } from "react";
import type {
  DocumentationSource,
  TOCEntry,
  ExportResponse,
} from "../types";
import {
  getTOC,
  enqueueExport,
  getExportJob,
  downloadExportZip,
  downloadExportFile,
} from "../api/client";
import { apiError } from "../api/errors";

interface Props {
  source: DocumentationSource;
}

export default function ExportPanel({ source }: Props) {
  const [toc, setToc] = useState<TOCEntry[]>([]);
  const [selectedTocIds, setSelectedTocIds] = useState<Set<string>>(new Set());
  const [topicQuery, setTopicQuery] = useState("");
  const [splitBy, setSplitBy] = useState<"" | "size" | "articles" | "tokens">("");
  const [splitValue, setSplitValue] = useState(50);
  const [respectChapters, setRespectChapters] = useState(false);
  const [includeImages, setIncludeImages] = useState(true);
  const [format, setFormat] = useState<"markdown" | "pdf">("markdown");
  const [exporting, setExporting] = useState(false);
  const [jobStatusMsg, setJobStatusMsg] = useState<string | null>(null);
  const [exportResult, setExportResult] = useState<ExportResponse | null>(null);
  const [error, setError] = useState("");
  const [mode, setMode] = useState<"full" | "chapters" | "topic">("full");
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (source.status === "completed") {
      getTOC(source.id)
        .then((data) => setToc(data.entries))
        .catch(() => setError("Failed to load table of contents"));
    }
  }, [source.id, source.status]);

  // Downloads must fetch through the authenticated API client (a plain link omits
  // the bearer token → 401). Surface any failure instead of saving a broken file.
  async function download(fn: () => Promise<void>) {
    try {
      await fn();
    } catch {
      setError("Download failed — please try again.");
    }
  }

  useEffect(() => {
    return () => {
      if (pollIntervalRef.current !== null) {
        clearInterval(pollIntervalRef.current);
      }
    };
  }, []);

  const toggleTocEntry = (id: string) => {
    const next = new Set(selectedTocIds);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    setSelectedTocIds(next);
  };

  const stopPolling = () => {
    if (pollIntervalRef.current !== null) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
  };

  const handleExport = async () => {
    setExporting(true);
    setError("");
    setExportResult(null);
    setJobStatusMsg("Queued…");
    stopPolling();

    let jobId: string;
    try {
      const created = await enqueueExport({
        source_id: source.id,
        toc_entry_ids:
          mode === "chapters" && selectedTocIds.size > 0
            ? Array.from(selectedTocIds)
            : undefined,
        topic_query: mode === "topic" ? topicQuery || undefined : undefined,
        split_by: splitBy || undefined,
        max_articles_per_file:
          splitBy === "articles" ? splitValue : undefined,
        max_file_size_bytes:
          splitBy === "size" ? splitValue * 1024 * 1024 : undefined,
        max_tokens_per_file:
          splitBy === "tokens" ? splitValue : undefined,
        respect_chapters: splitBy ? respectChapters : undefined,
        format,
        include_images: format === "pdf" ? undefined : includeImages,
      });
      jobId = created.export_job_id;
    } catch (e) {
      setError(apiError(e, "Export failed"));
      setExporting(false);
      setJobStatusMsg(null);
      return;
    }

    pollIntervalRef.current = setInterval(async () => {
      try {
        const job = await getExportJob(jobId);
        if (job.status === "pending") {
          setJobStatusMsg("Queued…");
        } else if (job.status === "running") {
          setJobStatusMsg("Generating…");
        } else if (job.status === "completed") {
          stopPolling();
          setJobStatusMsg(null);
          setExporting(false);
          setExportResult({
            export_id: job.export_id!,
            source_id: job.source_id,
            file_count: job.files?.length ?? 0,
            total_articles: job.files?.reduce((s, f) => s + f.article_count, 0) ?? 0,
            total_size_bytes: job.files?.reduce((s, f) => s + f.size_bytes, 0) ?? 0,
            zip_filename: job.zip_filename ?? "",
            files: job.files ?? [],
          });
        } else if (job.status === "failed" || job.status === "cancelled") {
          stopPolling();
          setJobStatusMsg(null);
          setExporting(false);
          setError(job.error_message || "Export failed");
        }
      } catch {
        stopPolling();
        setJobStatusMsg(null);
        setExporting(false);
        setError("Export failed");
      }
    }, 2000);
  };

  const renderTocTree = (entries: TOCEntry[], depth = 0) => {
    return entries.map((entry) => (
      <div key={entry.id} style={{ marginLeft: depth * 20 }}>
        <label className="toc-item">
          <input
            type="checkbox"
            checked={selectedTocIds.has(entry.id)}
            onChange={() => toggleTocEntry(entry.id)}
            disabled={mode !== "chapters"}
          />
          <span className={entry.is_article ? "toc-article" : "toc-section"}>
            {entry.is_article ? "📄 " : "📁 "}
            {entry.title}
          </span>
        </label>
        {entry.children.length > 0 && renderTocTree(entry.children, depth + 1)}
      </div>
    ));
  };

  if (source.status !== "completed") {
    return (
      <div className="export-panel">
        <h2>Export</h2>
        <p className="hint">
          Extraction must be completed before exporting. Current status:{" "}
          {source.status}.
        </p>
      </div>
    );
  }

  return (
    <div className="export-panel">
      <h2>Export — {source.name}</h2>

      {error && <div className="error">{error}</div>}

      <div className="export-mode">
        <label>
          <input
            type="radio"
            name="mode"
            checked={mode === "full"}
            onChange={() => setMode("full")}
          />
          Full export
        </label>
        <label>
          <input
            type="radio"
            name="mode"
            checked={mode === "chapters"}
            onChange={() => setMode("chapters")}
          />
          By chapters/sections
        </label>
        <label>
          <input
            type="radio"
            name="mode"
            checked={mode === "topic"}
            onChange={() => setMode("topic")}
          />
          By topic search
        </label>
      </div>

      {mode === "chapters" && (
        <div className="toc-selector">
          <h3>Select chapters/sections:</h3>
          <button
            className="btn-link"
            onClick={() => {
              const allIds = new Set(
                toc.flatMap((e) => [e.id, ...collectChildIds(e)])
              );
              setSelectedTocIds(allIds);
            }}
          >
            Select all
          </button>
          <button
            className="btn-link"
            onClick={() => setSelectedTocIds(new Set())}
          >
            Clear
          </button>
          <div className="toc-tree">{renderTocTree(toc)}</div>
        </div>
      )}

      {mode === "topic" && (
        <div className="topic-search">
          <input
            type="text"
            placeholder="Full-text search, e.g. backup retention policy"
            value={topicQuery}
            onChange={(e) => setTopicQuery(e.target.value)}
          />
          <p className="hint">
            Full-text search across titles and content — matching pages are
            exported most-relevant first.
          </p>
        </div>
      )}

      <div className="split-options">
        <h3>Format:</h3>
        <select
          value={format}
          onChange={(e) => setFormat(e.target.value as "markdown" | "pdf")}
        >
          <option value="markdown">Markdown</option>
          <option value="pdf">PDF</option>
        </select>
        {format === "markdown" && (
          <label className="chapter-toggle">
            <input
              type="checkbox"
              checked={includeImages}
              onChange={(e) => setIncludeImages(e.target.checked)}
            />
            Include images
            <span className="hint">
              {" "}— bundle images in a zip; uncheck for a smaller markdown-only
              export (image-heavy sources can be very large)
            </span>
          </label>
        )}
      </div>

      <div className="split-options">
        <h3>File splitting (optional):</h3>
        <select
          value={splitBy}
          onChange={(e) => setSplitBy(e.target.value as "" | "size" | "articles" | "tokens")}
        >
          <option value="">No splitting — single file</option>
          <option value="articles">Split by article count</option>
          <option value="size">Split by file size (MB)</option>
          <option value="tokens">Split by token count</option>
        </select>
        {splitBy && (
          <input
            type="number"
            min={1}
            value={splitValue}
            onChange={(e) => setSplitValue(Number(e.target.value))}
            placeholder={
              splitBy === "articles"
                ? "Articles per file"
                : splitBy === "size"
                ? "MB per file"
                : "Tokens per file"
            }
          />
        )}
        {splitBy && (
          <label className="chapter-toggle">
            <input
              type="checkbox"
              checked={respectChapters}
              onChange={(e) => setRespectChapters(e.target.checked)}
            />
            Keep chapters together
            <span className="hint"> — never split a chapter across files (files may be smaller)</span>
          </label>
        )}
      </div>

      <button
        className="btn-primary"
        onClick={handleExport}
        disabled={exporting}
      >
        {exporting
          ? (jobStatusMsg ?? "Queued…")
          : `Generate ${format === "pdf" ? "PDF" : "Markdown"} Export`}
      </button>

      {exportResult && (
        <div className="export-result">
          <h3>Export Complete</h3>
          <p>
            {exportResult.total_articles} articles in {exportResult.file_count}{" "}
            file(s) —{" "}
            {(exportResult.total_size_bytes / 1024).toFixed(1)} KB total
          </p>
          {exportResult.zip_filename && (
            <p>
              <button
                type="button"
                className="btn-primary"
                onClick={() => download(() => downloadExportZip(exportResult.export_id))}
              >
                Download ZIP (markdown + images)
              </button>
            </p>
          )}
          <ul>
            {exportResult.files.map((f, i) => (
              <li key={f.filename}>
                <button
                  type="button"
                  className={
                    !exportResult.zip_filename && i === 0 ? "btn-primary" : "link-btn"
                  }
                  onClick={() =>
                    download(() => downloadExportFile(exportResult.export_id, f.filename))
                  }
                >
                  {f.filename}
                </button>
                <span className="sub">
                  ({f.article_count} articles,{" "}
                  {(f.size_bytes / 1024).toFixed(1)} KB)
                </span>
              </li>
            ))}
          </ul>
          <p className="sub">
            Past exports are available under <strong>Exports</strong> in the menu.
          </p>
        </div>
      )}
    </div>
  );
}

function collectChildIds(entry: TOCEntry): string[] {
  const ids = entry.children.map((c) => c.id);
  for (const child of entry.children) {
    ids.push(...collectChildIds(child));
  }
  return ids;
}
