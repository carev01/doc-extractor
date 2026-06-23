import { useState, useEffect, useCallback, useRef } from "react";
import type { ExtractionRun, ScheduleListItem, ExportJobItem } from "../types";
import {
  listRuns,
  listSchedules,
  getRunLogs,
  listExportJobs,
  cancelExportJob,
} from "../api/client";

const STATUS_COLORS: Record<string, string> = {
  pending: "#6f8087",
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

function fmtDuration(fromIso: string | null, toIso: string | null): string {
  if (!fromIso) return "—";
  const from = new Date(fromIso).getTime();
  const to = toIso ? new Date(toIso).getTime() : Date.now();
  let s = Math.max(0, Math.round((to - from) / 1000));
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  s -= m * 60;
  return h > 0 ? `${h}h ${m}m ${s}s` : m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function pctOf(run: ExtractionRun): number | null {
  if (!run.articles_total || run.articles_total <= 0) return null;
  return Math.min(100, Math.round((run.articles_extracted / run.articles_total) * 100));
}

function path(run: ExtractionRun): string {
  return [run.vendor_name, run.product_name, run.source_name]
    .filter(Boolean)
    .join(" › ");
}

const ACTIVE = new Set(["running", "pending"]);

export default function JobsView() {
  const [runs, setRuns] = useState<ExtractionRun[]>([]);
  const [schedules, setSchedules] = useState<ScheduleListItem[]>([]);
  const [exportJobs, setExportJobs] = useState<ExportJobItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [r, s, e] = await Promise.all([
        listRuns(undefined, undefined, 200),
        listSchedules(),
        listExportJobs(undefined, 100),
      ]);
      setRuns(r.runs);
      setSchedules(s.schedules);
      setExportJobs(e.jobs);
    } catch {
      setError("Failed to load jobs");
    }
  }, []);

  const cancelExport = async (id: string) => {
    try {
      await cancelExportJob(id);
      await refresh();
    } catch {
      setError("Failed to cancel export");
    }
  };

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 4000);
    return () => clearInterval(id);
  }, [refresh]);

  const selected = runs.find((r) => r.id === selectedId) || null;
  if (selected) {
    return <RunDetail run={selected} onBack={() => setSelectedId(null)} />;
  }

  const active = runs.filter((r) => ACTIVE.has(r.status));
  const recent = runs.filter((r) => !ACTIVE.has(r.status));
  const enabledSchedules = schedules.filter((s) => s.enabled);

  return (
    <div className="jobs-view">
      <h2>Jobs</h2>
      {error && <div className="error">{error}</div>}

      <section className="jobs-section">
        <h3>Active &amp; queued ({active.length})</h3>
        {active.length === 0 && <p className="empty">Nothing running.</p>}
        <ul className="item-list">
          {active.map((run) => {
            const pct = pctOf(run);
            return (
              <li key={run.id} onClick={() => setSelectedId(run.id)}>
                <div className="item-info">
                  <strong>{path(run)}</strong>
                  <div className="item-meta">
                    {statusBadge(run.status)}
                    <span className="sub">{run.current_phase || "—"}</span>
                    <span className="sub">{run.trigger}</span>
                    <span className="sub">elapsed {fmtDuration(run.started_at, null)}</span>
                  </div>
                  <span className="sub">
                    {run.articles_extracted} / {run.articles_total || "?"} articles
                    {pct !== null ? ` (${pct}%)` : ""}
                  </span>
                  {pct !== null && (
                    <div className="progress-bar">
                      <div className="progress-fill" style={{ width: `${pct}%` }} />
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      </section>

      <section className="jobs-section">
        <h3>Scheduled ({enabledSchedules.length})</h3>
        {enabledSchedules.length === 0 && <p className="empty">No enabled schedules.</p>}
        <ul className="item-list">
          {enabledSchedules.map((s) => (
            <li key={s.source_id} className="non-clickable">
              <div className="item-info">
                <strong>{[s.vendor_name, s.product_name, s.source_name].join(" › ")}</strong>
                <div className="item-meta">
                  <span className="status-badge" style={{ backgroundColor: "#5a7fa3" }}>
                    scheduled
                  </span>
                  <span className="sub">{s.frequency} @ {s.time_of_day} {s.timezone}</span>
                </div>
                <span className="sub">
                  Next run: {s.next_run_at ? new Date(s.next_run_at).toLocaleString() : "—"}
                </span>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="jobs-section">
        <h3>Exports ({exportJobs.length})</h3>
        {exportJobs.length === 0 && <p className="empty">No export jobs.</p>}
        <ul className="item-list">
          {exportJobs.map((j) => (
            <li key={j.id} className="non-clickable">
              <div className="item-info">
                <strong>{[j.vendor_name, j.product_name, j.source_name].join(" › ")}</strong>
                <div className="item-meta">
                  {statusBadge(j.status)}
                  <span className="sub">{j.format}</span>
                  <span className="sub">
                    {j.created_at ? new Date(j.created_at).toLocaleString() : "—"}
                  </span>
                </div>
                {j.status === "failed" && j.error_message && (
                  <span className="sub">{j.error_message}</span>
                )}
              </div>
              {j.status === "pending" && (
                <div className="item-actions">
                  <button className="btn-danger-sm" onClick={() => cancelExport(j.id)}>
                    Cancel
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section className="jobs-section">
        <h3>Recent ({recent.length})</h3>
        {recent.length === 0 && <p className="empty">No past runs.</p>}
        <ul className="item-list">
          {recent.map((run) => (
            <li key={run.id} onClick={() => setSelectedId(run.id)}>
              <div className="item-info">
                <strong>{path(run)}</strong>
                <div className="item-meta">
                  {statusBadge(run.status)}
                  <span className="sub">{run.trigger}</span>
                  <span className="sub">
                    {run.started_at ? new Date(run.started_at).toLocaleString() : "—"}
                  </span>
                  <span className="sub">took {fmtDuration(run.started_at, run.completed_at)}</span>
                </div>
                <span className="sub">
                  {run.status === "failed" && run.error_message
                    ? run.error_message
                    : `${run.articles_extracted} new · ${run.articles_updated ?? 0} updated · ${run.articles_unchanged ?? 0} unchanged (of ${run.articles_total})`}
                </span>
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

function RunDetail({ run, onBack }: { run: ExtractionRun; onBack: () => void }) {
  const [tab, setTab] = useState<"overview" | "logs">("overview");
  const [logs, setLogs] = useState<string>("");
  const [loadingLogs, setLoadingLogs] = useState(false);
  const logBoxRef = useRef<HTMLPreElement | null>(null);
  const isActive = ACTIVE.has(run.status);

  const fetchLogs = useCallback(async () => {
    setLoadingLogs(true);
    try {
      const d = await getRunLogs(run.id);
      setLogs(d.log_text);
    } catch {
      setLogs("(failed to load logs)");
    } finally {
      setLoadingLogs(false);
    }
  }, [run.id]);

  // Load logs when the Logs tab opens; poll while the run is active.
  useEffect(() => {
    if (tab !== "logs") return;
    fetchLogs();
    if (!isActive) return;
    const id = setInterval(fetchLogs, 4000);
    return () => clearInterval(id);
  }, [tab, isActive, fetchLogs]);

  // Auto-scroll the log box to the newest line.
  useEffect(() => {
    if (logBoxRef.current) logBoxRef.current.scrollTop = logBoxRef.current.scrollHeight;
  }, [logs]);

  const pct = pctOf(run);

  return (
    <div className="jobs-view">
      <button className="link-btn" onClick={onBack}>← Back to Jobs</button>
      <h2>{path(run)}</h2>
      <div className="item-meta">
        {statusBadge(run.status)}
        <span className="sub">{run.trigger}</span>
        {run.attempts ? <span className="sub">attempt {run.attempts}</span> : null}
      </div>

      <nav className="source-tabs" style={{ marginTop: "1rem" }}>
        <button className={tab === "overview" ? "active" : ""} onClick={() => setTab("overview")}>
          Overview
        </button>
        <button className={tab === "logs" ? "active" : ""} onClick={() => setTab("logs")}>
          Logs
        </button>
      </nav>

      {tab === "overview" && (
        <div className="run-overview">
          {pct !== null && (
            <div className="progress-bar" style={{ margin: "0.6rem 0" }}>
              <div className="progress-fill" style={{ width: `${pct}%` }} />
            </div>
          )}
          <dl className="stat-grid">
            <div><dt>Progress</dt><dd>{pct !== null ? `${pct}%` : "—"}</dd></div>
            <div><dt>Processed / total</dt><dd>{run.articles_extracted} / {run.articles_total || "?"}</dd></div>
            <div><dt>New</dt><dd>{run.articles_extracted}</dd></div>
            <div><dt>Updated</dt><dd>{run.articles_updated ?? 0}</dd></div>
            <div><dt>Unchanged</dt><dd>{run.articles_unchanged ?? 0}</dd></div>
            <div><dt>Phase</dt><dd>{run.current_phase || "—"}</dd></div>
            <div><dt>Elapsed</dt><dd>{fmtDuration(run.started_at, run.completed_at)}</dd></div>
            <div><dt>Started</dt><dd>{run.started_at ? new Date(run.started_at).toLocaleString() : "—"}</dd></div>
            <div><dt>Completed</dt><dd>{run.completed_at ? new Date(run.completed_at).toLocaleString() : "—"}</dd></div>
          </dl>
          {run.error_message && <div className="error">{run.error_message}</div>}
        </div>
      )}

      {tab === "logs" && (
        <div className="run-logs">
          {loadingLogs && !logs && <p className="sub">Loading logs…</p>}
          <pre className="log-box" ref={logBoxRef}>{logs || "(no logs captured)"}</pre>
          {isActive && <p className="sub">Live — refreshing every 4s</p>}
        </div>
      )}
    </div>
  );
}
