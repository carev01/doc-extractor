import { useState, useEffect, useRef, useCallback } from "react";
import type {
  Product,
  DocumentationSource,
  ExtractionRun,
  Job,
  ProfileOption,
} from "../types";
import {
  listSources,
  createSource,
  deleteSource,
  updateSource,
  triggerExtraction,
  getRunStatus,
  listRuns,
  resanitizeSource,
  listJobs,
  getProfiles,
  assignSourceToJob,
  unassignSourceFromJob,
} from "../api/client";

// The platform options are fetched from the backend profile registry
// (GET /api/profiles) so the dropdown can't drift. This is only the fallback
// shown until that resolves (or if it fails): "auto" auto-detects, which is the
// safe default for any source.
const FALLBACK_PLATFORM_OPTIONS: ProfileOption[] = [
  { value: "auto", label: "Auto-detect" },
];

interface Props {
  product: Product;
  onSelectSource: (source: DocumentationSource) => void;
  selectedSourceId?: string;
}

const STATUS_COLORS: Record<string, string> = {
  pending: "#6f8087",
  extracting: "#eaa53d",
  running: "#eaa53d",
  completed: "#58c08a",
  failed: "#e0685f",
  cancelled: "#6f8087",
};

function statusBadge(status: string) {
  return (
    <span
      className="status-badge"
      style={{ backgroundColor: STATUS_COLORS[status] || "#888" }}
    >
      {status}
    </span>
  );
}

export default function SourceList({
  product,
  onSelectSource,
  selectedSourceId,
}: Props) {
  const [sources, setSources] = useState<DocumentationSource[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [platformOptions, setPlatformOptions] = useState<ProfileOption[]>(
    FALLBACK_PLATFORM_OPTIONS,
  );

  useEffect(() => {
    getProfiles()
      .then((opts) => {
        if (opts.length) setPlatformOptions(opts);
      })
      .catch(() => {
        /* non-fatal: keep the auto-detect fallback */
      });
  }, []);

  const fetchSources = useCallback(async () => {
    try {
      const data = await listSources(product.id);
      setSources(data.sources);
    } catch {
      setError("Failed to load sources");
    }
  }, [product.id]);

  const fetchJobs = useCallback(async () => {
    try {
      setJobs((await listJobs()).jobs);
    } catch {
      /* non-fatal: job dropdown just stays empty */
    }
  }, []);

  useEffect(() => {
    fetchSources();
    fetchJobs();
  }, [fetchSources, fetchJobs]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !baseUrl.trim()) return;
    setLoading(true);
    setError("");
    try {
      await createSource({
        product_id: product.id,
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

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this source and all extracted articles?")) return;
    try {
      await deleteSource(id);
      await fetchSources();
    } catch (e: any) {
      setError(e.response?.data?.detail || "Failed to delete source");
    }
  };

  return (
    <div className="source-list">
      <h2>Documentation Sources — {product.name}</h2>

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
          <SourceItem
            key={s.id}
            source={s}
            jobs={jobs}
            selected={s.id === selectedSourceId}
            onSelect={onSelectSource}
            onDelete={handleDelete}
            onSourceChanged={fetchSources}
            platformOptions={platformOptions}
          />
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

interface SourceItemProps {
  source: DocumentationSource;
  jobs: Job[];
  selected: boolean;
  onSelect: (source: DocumentationSource) => void;
  onDelete: (id: string) => void;
  onSourceChanged: () => void;
  platformOptions: ProfileOption[];
}

function SourceItem({
  source,
  jobs,
  selected,
  onSelect,
  onDelete,
  onSourceChanged,
  platformOptions,
}: SourceItemProps) {
  const [activeRun, setActiveRun] = useState<ExtractionRun | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [history, setHistory] = useState<ExtractionRun[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [itemError, setItemError] = useState("");
  const [resanitizing, setResanitizing] = useState(false);
  const [resanitizeMsg, setResanitizeMsg] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const isExtracting =
    source.status === "extracting" ||
    activeRun?.status === "running" ||
    activeRun?.status === "pending";

  const loadHistory = useCallback(async () => {
    try {
      const data = await listRuns(source.id);
      setHistory(data.runs.slice(0, 5));
    } catch {
      /* non-fatal */
    }
  }, [source.id]);

  // Load run history once on mount / when source changes.
  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  // Poll the active run's status every 3s while it is running.
  useEffect(() => {
    if (!runId) return;

    const poll = async () => {
      try {
        const run = await getRunStatus(runId);
        setActiveRun(run);
        if (run.status !== "running" && run.status !== "pending") {
          stopPolling();
          setRunId(null);
          await loadHistory();
          onSourceChanged();
        }
      } catch {
        stopPolling();
        setRunId(null);
      }
    };

    poll();
    pollRef.current = setInterval(poll, 3000);
    return stopPolling;
  }, [runId, stopPolling, loadHistory, onSourceChanged]);

  // Clean up any timer on unmount.
  useEffect(() => stopPolling, [stopPolling]);

  const handleExtract = async () => {
    setItemError("");
    try {
      const res = await triggerExtraction(source.id);
      setActiveRun(null);
      setRunId(res.run_id);
      onSourceChanged();
    } catch (e: any) {
      setItemError(e.response?.data?.detail || "Failed to trigger extraction");
    }
  };

  const handleJobChange = async (nextJobId: string) => {
    setItemError("");
    try {
      if (nextJobId) {
        await assignSourceToJob(nextJobId, source.id);
      } else if (source.job_id) {
        await unassignSourceFromJob(source.job_id, source.id);
      }
      onSourceChanged();
    } catch {
      setItemError("Failed to change job assignment");
    }
  };

  const handleResanitize = async () => {
    setItemError("");
    setResanitizeMsg("");
    setResanitizing(true);
    try {
      const res = await resanitizeSource(source.id);
      setResanitizeMsg(
        res.changed > 0
          ? `Re-sanitized ${res.changed} of ${res.total} articles.`
          : `All ${res.total} articles already clean.`
      );
      onSourceChanged();
    } catch (e: any) {
      setItemError(e.response?.data?.detail || "Failed to re-sanitize");
    } finally {
      setResanitizing(false);
    }
  };

  const renderRunResult = (run: ExtractionRun) => {
    if (run.status === "pending") {
      return (
        <div className="run-progress">
          <span className="run-phase run-pending">Queued…</span>
          <div className="progress-bar indeterminate" />
        </div>
      );
    }

    if (run.status === "running") {
      const processed =
        (run.articles_extracted ?? 0) +
        (run.articles_updated ?? 0) +
        (run.articles_unchanged ?? 0);
      const total = run.articles_total ?? 0;
      const pct = total > 0 ? Math.round((processed / total) * 100) : 0;

      if (run.current_phase === "toc_discovery") {
        return (
          <div className="run-progress">
            <span className="run-phase">Discovering table of contents…</span>
            <div className="progress-bar indeterminate" />
          </div>
        );
      }

      return (
        <div className="run-progress">
          <span className="run-phase">
            Scraping content
            {total > 0 ? ` — ${processed} / ${total} pages (${pct}%)` : "…"}
          </span>
          {total > 0 && (
            <div className="progress-bar">
              <div className="progress-fill" style={{ width: `${pct}%` }} />
            </div>
          )}
          <span className="run-counts sub">
            {run.articles_extracted ?? 0} new ·{" "}
            {run.articles_updated ?? 0} updated ·{" "}
            {run.articles_unchanged ?? 0} unchanged
          </span>
        </div>
      );
    }

    if (run.status === "failed") {
      return (
        <span className="sub run-failed">
          Failed{run.error_message ? `: ${run.error_message}` : ""}
        </span>
      );
    }

    // completed
    const parts = [`${run.articles_extracted} new`];
    if (typeof run.articles_updated === "number")
      parts.push(`${run.articles_updated} updated`);
    if (typeof run.articles_unchanged === "number")
      parts.push(`${run.articles_unchanged} unchanged`);
    return (
      <span className="sub run-done">
        Done — {parts.join(", ")} (of {run.articles_total})
      </span>
    );
  };

  return (
    <li
      className={selected ? "selected" : ""}
      onClick={() => onSelect(source)}
    >
      <div className="item-info">
        <strong>{source.name}</strong>
        <span className="sub">{source.base_url}</span>
        <div className="item-meta">
          {statusBadge(source.status)}
          {source.last_extracted_at && (
            <span className="sub">
              Last: {new Date(source.last_extracted_at).toLocaleString()}
            </span>
          )}
        </div>

        <div className="item-meta">
          <label className="sub" style={{ display: "flex", alignItems: "center", gap: "0.4em" }}>
            Platform:
            <select
              value={source.platform ?? "auto"}
              onClick={(e) => e.stopPropagation()}
              onChange={async (e) => {
                e.stopPropagation();
                try {
                  await updateSource(source.id, { platform: e.target.value });
                  onSourceChanged();
                } catch {
                  /* non-fatal: parent will re-render on next refresh */
                }
              }}
            >
              {platformOptions.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn-secondary-sm"
              title="Clear cached LLM-derived profile; re-derives on next extraction"
              onClick={async (e) => {
                e.stopPropagation();
                try {
                  await updateSource(source.id, { refresh_profile: true });
                  onSourceChanged();
                } catch {
                  /* non-fatal */
                }
              }}
            >
              ↻ Re-derive
            </button>
          </label>
        </div>

        <div className="item-meta">
          <label className="sub" style={{ display: "flex", alignItems: "center", gap: "0.4em" }}>
            Job:
            <select
              value={source.job_id ?? ""}
              onClick={(e) => e.stopPropagation()}
              onChange={(e) => {
                e.stopPropagation();
                handleJobChange(e.target.value);
              }}
            >
              <option value="">(none — manual only)</option>
              {jobs.map((j) => (
                <option key={j.id} value={j.id}>
                  {j.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {itemError && <div className="error">{itemError}</div>}
        {resanitizeMsg && <span className="sub run-done">{resanitizeMsg}</span>}
        {activeRun && (
          <div className="run-status">{renderRunResult(activeRun)}</div>
        )}

        {history.length > 0 && (
          <div className="run-history">
            <button
              type="button"
              className="link-btn"
              onClick={(e) => {
                e.stopPropagation();
                setShowHistory((v) => !v);
              }}
            >
              {showHistory ? "▾" : "▸"} Run history ({history.length})
            </button>
            {showHistory && (
              <ul className="run-history-list">
                {history.map((r) => (
                  <li key={r.id}>
                    {statusBadge(r.status)}{" "}
                    <span className="sub">
                      {r.started_at
                        ? new Date(r.started_at).toLocaleString()
                        : "—"}
                      {" · "}
                      {r.articles_extracted}/{r.articles_total}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      <div className="item-actions">
        <button
          className="btn-primary-sm"
          onClick={(e) => {
            e.stopPropagation();
            handleExtract();
          }}
          disabled={isExtracting}
        >
          {isExtracting ? "Extracting..." : "Extract"}
        </button>
        <button
          className="btn-secondary-sm"
          title="Re-apply the current sanitizer to already-stored articles"
          disabled={resanitizing || isExtracting}
          onClick={(e) => {
            e.stopPropagation();
            handleResanitize();
          }}
        >
          {resanitizing ? "Cleaning…" : "Re-sanitize"}
        </button>
        <button
          className="btn-secondary-sm"
          title="Rename"
          onClick={async (e) => {
            e.stopPropagation();
            const next = prompt("Rename source", source.name);
            if (next === null || !next.trim() || next.trim() === source.name) return;
            try {
              await updateSource(source.id, { name: next.trim() });
              onSourceChanged();
            } catch {
              setItemError("Failed to rename source");
            }
          }}
        >
          ✎
        </button>
        <button
          className="btn-danger-sm"
          onClick={(e) => {
            e.stopPropagation();
            onDelete(source.id);
          }}
        >
          ×
        </button>
      </div>
    </li>
  );
}
