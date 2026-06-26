import { useState, useEffect, useCallback } from "react";
import type { Job, JobRunItem, Frequency } from "../types";
import {
  listJobs,
  createJob,
  updateJob,
  deleteJob,
  runJob,
  listJobRuns,
  unassignSourceFromJob,
} from "../api/client";
import SourcePicker from "./SourcePicker";

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

const RUN_STATUS_COLORS: Record<string, string> = {
  pending: "#6f8087",
  running: "#eaa53d",
  completed: "#58c08a",
  partial: "#c8923d",
  failed: "#e0685f",
  cancelled: "#6f8087",
};

function scheduleSummary(job: Job): string {
  if (!job.enabled || !job.frequency) return "Manual only";
  const at = job.time_of_day ?? "02:00";
  switch (job.frequency) {
    case "hourly":
      return `Hourly at :${at.split(":")[1]}`;
    case "daily":
      return `Daily at ${at}`;
    case "weekly":
      return `Weekly on ${DAYS[job.day_of_week ?? 0]} at ${at}`;
    case "monthly":
      return `Monthly on day ${job.day_of_month ?? 1} at ${at}`;
    default:
      return job.frequency;
  }
}

export default function JobsManager() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [newName, setNewName] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const data = await listJobs();
      setJobs(data.jobs);
    } catch {
      setError("Failed to load jobs");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;
    setError("");
    try {
      await createJob({ name: newName.trim() });
      setNewName("");
      await refresh();
    } catch (e: any) {
      setError(e.response?.data?.detail || "Failed to create job");
    }
  };

  return (
    <div className="jobs-manager">
      {error && <div className="error">{error}</div>}

      <form onSubmit={handleCreate} className="add-form">
        <input
          type="text"
          placeholder="New job name (e.g. 'Nightly backups')"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          required
        />
        <button type="submit">Create job</button>
      </form>

      <ul className="item-list">
        {jobs.map((job) => (
          <JobCard key={job.id} job={job} onChanged={refresh} onError={setError} />
        ))}
        {jobs.length === 0 && (
          <li className="empty">No jobs yet. Create one above, then assign sources to it.</li>
        )}
      </ul>
    </div>
  );
}

function JobCard({
  job,
  onChanged,
  onError,
}: {
  job: Job;
  onChanged: () => void;
  onError: (msg: string) => void;
}) {
  // Local draft of the schedule fields so edits don't fire a request per keystroke.
  const [enabled, setEnabled] = useState(job.enabled);
  const [frequency, setFrequency] = useState<Frequency>(job.frequency ?? "daily");
  const [timeOfDay, setTimeOfDay] = useState(job.time_of_day ?? "02:00");
  const [dayOfWeek, setDayOfWeek] = useState(job.day_of_week ?? 0);
  const [dayOfMonth, setDayOfMonth] = useState(job.day_of_month ?? 1);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showRuns, setShowRuns] = useState(false);
  const [runs, setRuns] = useState<JobRunItem[]>([]);
  const [msg, setMsg] = useState("");
  const [showPicker, setShowPicker] = useState(false);

  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

  const dirty =
    enabled !== job.enabled ||
    frequency !== (job.frequency ?? "daily") ||
    timeOfDay !== (job.time_of_day ?? "02:00") ||
    dayOfWeek !== (job.day_of_week ?? 0) ||
    dayOfMonth !== (job.day_of_month ?? 1);

  const saveSchedule = async () => {
    setSaving(true);
    onError("");
    try {
      await updateJob(job.id, {
        enabled,
        frequency,
        time_of_day: timeOfDay,
        day_of_week: dayOfWeek,
        day_of_month: dayOfMonth,
        timezone: browserTz,
      });
      onChanged();
    } catch (e: any) {
      onError(e.response?.data?.detail || "Failed to save schedule");
    } finally {
      setSaving(false);
    }
  };

  const rename = async () => {
    const next = prompt("Rename job", job.name);
    if (next === null || !next.trim() || next.trim() === job.name) return;
    try {
      await updateJob(job.id, { name: next.trim() });
      onChanged();
    } catch {
      onError("Failed to rename job");
    }
  };

  const remove = async () => {
    if (!confirm(`Delete job "${job.name}"? Its sources will be un-assigned (not deleted).`)) return;
    try {
      await deleteJob(job.id);
      onChanged();
    } catch {
      onError("Failed to delete job");
    }
  };

  const run = async () => {
    setBusy(true);
    setMsg("");
    onError("");
    try {
      const jr = await runJob(job.id);
      setMsg(
        jr.sources_total > 0
          ? `Queued ${jr.sources_total} source${jr.sources_total === 1 ? "" : "s"}.`
          : "Nothing to run (all sources already active)."
      );
      onChanged();
    } catch (e: any) {
      onError(e.response?.data?.detail || "Failed to run job");
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

  const unassign = async (sourceId: string) => {
    try {
      await unassignSourceFromJob(job.id, sourceId);
      onChanged();
    } catch {
      onError("Failed to un-assign source");
    }
  };

  return (
    <li className="non-clickable job-card">
      <div className="item-info">
        <div className="item-meta">
          <strong>{job.name}</strong>
          <span className="status-badge" style={{ backgroundColor: enabled ? "#5a7fa3" : "#6f8087" }}>
            {scheduleSummary(job)}
          </span>
          {job.enabled && job.next_run_at && (
            <span className="sub">Next: {new Date(job.next_run_at).toLocaleString()}</span>
          )}
          <span className="sub">{job.source_count} source{job.source_count === 1 ? "" : "s"}</span>
        </div>

        {/* Schedule editor */}
        <div className="job-schedule">
          <label className="sub" style={{ display: "flex", alignItems: "center", gap: "0.4em" }}>
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            Run on a schedule
          </label>
          {enabled && (
            <div className="schedule-fields">
              <select value={frequency} onChange={(e) => setFrequency(e.target.value as Frequency)}>
                <option value="hourly">Hourly</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
              </select>
              {frequency === "weekly" && (
                <select value={dayOfWeek} onChange={(e) => setDayOfWeek(Number(e.target.value))}>
                  {DAYS.map((d, i) => <option key={i} value={i}>{d}</option>)}
                </select>
              )}
              {frequency === "monthly" && (
                <select value={dayOfMonth} onChange={(e) => setDayOfMonth(Number(e.target.value))}>
                  {Array.from({ length: 28 }, (_, i) => i + 1).map((d) => (
                    <option key={d} value={d}>{d}</option>
                  ))}
                </select>
              )}
              {frequency !== "hourly" && (
                <input type="time" value={timeOfDay} onChange={(e) => setTimeOfDay(e.target.value)} />
              )}
              {frequency === "hourly" && (
                <span className="hint">at minute {timeOfDay.split(":")[1]}</span>
              )}
              <span className="schedule-tz">{browserTz}</span>
            </div>
          )}
          {dirty && (
            <button className="btn-primary-sm" disabled={saving} onClick={saveSchedule}>
              {saving ? "Saving…" : "Save schedule"}
            </button>
          )}
        </div>

        {/* Assigned sources */}
        {job.sources.length > 0 && (
          <ul className="job-sources">
            {job.sources.map((s) => (
              <li key={s.id}>
                <span className="sub">{[s.vendor_name, s.product_name, s.name].join(" › ")}</span>
                <button className="link-btn" title="Un-assign" onClick={() => unassign(s.id)}>×</button>
              </li>
            ))}
          </ul>
        )}
        {job.sources.length === 0 && (
          <span className="sub">No sources assigned — assign them from a product's source list.</span>
        )}

        {msg && <span className="sub run-done">{msg}</span>}

        {/* Recent runs */}
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
        <button className="btn-primary-sm" disabled={busy || job.source_count === 0} onClick={run}>
          {busy ? "Running…" : "Run now"}
        </button>
        <button className="btn-secondary-sm" onClick={() => setShowPicker(true)}>
          Add sources
        </button>
        <button className="btn-secondary-sm" title="Rename" onClick={rename}>✎</button>
        <button className="btn-danger-sm" title="Delete" onClick={remove}>×</button>
      </div>
      {showPicker && (
        <SourcePicker
          jobId={job.id}
          onClose={() => setShowPicker(false)}
          onAssigned={onChanged}
        />
      )}
    </li>
  );
}
