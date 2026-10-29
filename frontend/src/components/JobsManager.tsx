import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import type { CatalogVendor, Job, JobRunItem, Tier } from "../types";
import { listJobs, deleteJob, runJob, listJobRuns, listTiers, getJobCatalog } from "../api/client";
import { apiError } from "../api/errors";
import JobEditor from "./JobEditor";
import {
  type CatalogIndex,
  coverageLine,
  fmtInTz,
  indexCatalog,
  overlapWith,
  resolveJob,
  resolveTargets,
  scheduleSummary,
} from "./jobHelpers";

const RUN_STATUS_COLORS: Record<string, string> = {
  pending: "#6f8087",
  running: "#eaa53d",
  completed: "#58c08a",
  partial: "#c8923d",
  failed: "#e0685f",
  cancelled: "#6f8087",
};

const SKIP_REASON: Record<string, string> = {
  active_run: "already queued or running",
  paused_run: "paused run waiting",
  login_needed: "login needed",
  login_failed: "login failed",
};

/** Jobs list; "New job" / "Edit" open the job editor dialog. */
export default function JobsManager() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [tiers, setTiers] = useState<Tier[]>([]);
  // null until loaded. The editor opens regardless but keeps its tree disabled
  // (and Save off) until this open's fetch resolves, so an empty or stale
  // catalog can never be read as an edit of the job's picks.
  const [catalog, setCatalog] = useState<CatalogVendor[] | null>(null);
  // undefined = closed, null = creating, Job = editing.
  const [editing, setEditing] = useState<Job | null | undefined>(undefined);
  const [error, setError] = useState("");
  // Whether `catalog` is from the current open's fetch (it may be a stale earlier one).
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogError, setCatalogError] = useState("");
  const catalogSeq = useRef(0);

  const refresh = useCallback(async () => {
    try {
      setJobs((await listJobs()).jobs);
    } catch (e) {
      setError(apiError(e, "Failed to load jobs"));
    }
  }, []);

  // Tiers and the catalog change elsewhere (Tier manager, vendor list), so
  // they are re-read each time the editor opens.
  const loadContext = useCallback(() => {
    listTiers()
      .then(setTiers)
      .catch((e) => setError(apiError(e, "Failed to load tiers")));
    const seq = ++catalogSeq.current;
    getJobCatalog()
      .then((c) => {
        if (seq !== catalogSeq.current) return;
        setCatalog(c);
        setCatalogLoading(false);
      })
      .catch((e) => {
        if (seq !== catalogSeq.current) return;
        setCatalogError(apiError(e, "Failed to load sources"));
        setCatalogLoading(false);
      });
  }, []);

  useEffect(() => {
    listJobs()
      .then((data) => setJobs(data.jobs))
      .catch((e) => setError(apiError(e, "Failed to load jobs")));
    loadContext();
  }, [loadContext]);

  const idx = useMemo(() => (catalog ? indexCatalog(catalog) : null), [catalog]);

  // Marks the catalog stale before re-reading it (kept out of loadContext, which
  // the mount effect calls and must not set state synchronously).
  const reloadContext = () => {
    setCatalogLoading(true);
    setCatalogError("");
    loadContext();
  };

  const open = (job: Job | null) => {
    setError("");
    reloadContext();
    setEditing(job);
  };

  return (
    <div className="jobs-manager">
      {error && <div className="error">{error}</div>}

      <div className="add-form">
        <button type="button" className="btn-primary" onClick={() => open(null)}>
          New job
        </button>
      </div>

      <ul className="item-list">
        {jobs.map((job) => (
          <JobCard
            key={job.id}
            job={job}
            jobs={jobs}
            tiers={tiers}
            idx={idx}
            onEdit={() => open(job)}
            onChanged={refresh}
            onError={setError}
          />
        ))}
        {jobs.length === 0 && <li className="empty">No jobs yet. Create one above.</li>}
      </ul>

      {editing !== undefined && (
        <JobEditor
          key={editing?.id ?? "new"}
          job={editing}
          jobs={jobs}
          tiers={tiers}
          catalog={catalog ?? []}
          catalogLoading={catalogLoading}
          catalogError={catalogError}
          onRetryCatalog={reloadContext}
          onClose={() => setEditing(undefined)}
          onSaved={refresh}
        />
      )}
    </div>
  );
}

function JobCard({
  job,
  jobs,
  tiers,
  idx,
  onEdit,
  onChanged,
  onError,
}: {
  job: Job;
  jobs: Job[];
  tiers: Tier[];
  /** Null until the catalog has loaded (overlap names need it). */
  idx: CatalogIndex | null;
  onEdit: () => void;
  onChanged: () => void;
  onError: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [showRuns, setShowRuns] = useState(false);
  const [runs, setRuns] = useState<JobRunItem[]>([]);
  const [msg, setMsg] = useState("");

  const tier = tiers.find((t) => t.id === job.tier_id);
  const overlapNames = useMemo(() => {
    if (!idx || job.coverage.overlap_sources === 0) return [];
    const mine = resolveJob(job.tier_id, resolveTargets(job.targets, idx), idx);
    return overlapWith(mine, job.id, jobs, idx).names;
  }, [idx, job, jobs]);

  const remove = async () => {
    if (!confirm(`Delete job "${job.name}"? Its sources are not affected.`)) return;
    try {
      await deleteJob(job.id);
      onChanged();
    } catch (e) {
      onError(apiError(e, "Failed to delete job"));
    }
  };

  const run = async () => {
    setBusy(true);
    setMsg("");
    onError("");
    try {
      const jr = await runJob(job.id);
      const skipped = jr.skipped.length;
      const reasons = [...new Set(jr.skipped.map((s) => SKIP_REASON[s.reason] ?? s.reason))];
      setMsg(
        `Queued ${jr.sources_total} source(s)` +
          (skipped ? `; skipped ${skipped} (${reasons.join(", ")})` : "") +
          ".",
      );
      onChanged();
    } catch (e) {
      onError(apiError(e, "Failed to run job"));
    } finally {
      setBusy(false);
    }
  };

  const toggleRuns = async () => {
    const next = !showRuns;
    setShowRuns(next);
    if (next) {
      try {
        setRuns(await listJobRuns(job.id));
      } catch {
        /* non-fatal */
      }
    }
  };

  return (
    <li className="non-clickable job-card">
      <div className="item-info">
        <div className="item-meta">
          <strong>{job.name}</strong>
          <span className="status-badge" style={{ backgroundColor: job.enabled ? "#58c08a" : "#6f8087" }}>
            {job.enabled ? "enabled" : "disabled"}
          </span>
          {tier && <span className="sub">Tier: {tier.name}</span>}
        </div>
        <span className="sub">{scheduleSummary(job)}</span>
        {job.next_runs.length > 0 && (
          <span className="sub">
            Next: {job.next_runs.map((r) => fmtInTz(r, job.timezone)).join(" · ")}
          </span>
        )}
        <span className="sub">{coverageLine(job.coverage, overlapNames)}</span>
        {msg && <span className="sub run-done">{msg}</span>}

        <div className="run-history">
          <button type="button" className="link-btn" onClick={toggleRuns}>
            {showRuns ? "▾" : "▸"} Recent runs
          </button>
          {showRuns && (
            <ul className="run-history-list">
              {runs.length === 0 && <li className="sub">No runs yet.</li>}
              {runs.map((r) => (
                <li key={r.id}>
                  <span className="status-badge" style={{ backgroundColor: RUN_STATUS_COLORS[r.status] || "#888" }}>
                    {r.status}
                  </span>{" "}
                  <span className="sub">
                    {r.trigger} · {r.sources_done}/{r.sources_total} done
                    {r.sources_failed > 0 ? `, ${r.sources_failed} failed` : ""}
                    {r.children_paused > 0 ? `, ${r.children_paused} paused` : ""}
                    {r.skipped.length > 0 ? `, ${r.skipped.length} skipped` : ""}
                    {" · "}
                    {r.created_at ? new Date(r.created_at).toLocaleString() : "—"}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="item-actions">
        <button className="btn-primary-sm" disabled={busy || job.coverage.sources === 0} onClick={run}>
          {busy ? "Running…" : "Run now"}
        </button>
        <button className="btn-secondary-sm" onClick={onEdit}>
          Edit
        </button>
        <button className="btn-danger-sm" title="Delete" onClick={remove}>
          ×
        </button>
      </div>
    </li>
  );
}
