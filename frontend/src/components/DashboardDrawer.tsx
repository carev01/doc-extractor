import { useEffect, useState } from "react";
import type { ExtractionRun, OverviewSourceRow } from "../types";
import { listRuns, enrichSource, retryEscalation } from "../api/client";
import { apiError } from "../api/errors";

const STATUS_COLORS: Record<string, string> = {
  pending: "#6f8087",
  running: "#eaa53d",
  completed: "#58c08a",
  failed: "#e0685f",
  cancelled: "#6f8087",
  paused: "#6f8087",
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

interface Props {
  row: OverviewSourceRow;
  onClose: () => void;
  onAction: () => void;
  onOpenFull: (row: OverviewSourceRow) => void;
}

export default function DashboardDrawer({ row, onClose, onAction, onOpenFull }: Props) {
  const [runs, setRuns] = useState<ExtractionRun[] | null>(null);
  const [runsError, setRunsError] = useState("");

  const [enriching, setEnriching] = useState(false);
  const [enrMsg, setEnrMsg] = useState("");

  const [escalating, setEscalating] = useState(false);
  const [escMsg, setEscMsg] = useState("");

  // Load recent runs on mount. The parent mounts a fresh instance (keyed by
  // row.id) whenever a different row is selected, so this only ever runs once
  // per row and local state (runs/messages) starts clean without a reset here.
  useEffect(() => {
    let cancelled = false;
    listRuns(row.id, undefined, 10)
      .then((data) => {
        if (!cancelled) setRuns(data.runs);
      })
      .catch(() => {
        if (!cancelled) setRunsError("Failed to load recent runs");
      });
    return () => {
      cancelled = true;
    };
  }, [row.id]);

  // ESC closes the drawer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const handleEnrich = async () => {
    setEnrMsg("");
    setEnriching(true);
    try {
      await enrichSource(row.id);
      setEnrMsg("Enrichment queued");
      onAction();
    } catch (e) {
      setEnrMsg(apiError(e, "Failed to queue enrichment"));
    } finally {
      setEnriching(false);
    }
  };

  const handleRetryEscalation = async () => {
    if (!row.escalation.run_id) return;
    setEscMsg("");
    setEscalating(true);
    try {
      await retryEscalation(row.escalation.run_id);
      setEscMsg("Escalation retry queued");
      onAction();
    } catch (e) {
      setEscMsg(apiError(e, "Failed to queue escalation retry"));
    } finally {
      setEscalating(false);
    }
  };

  const showEscalation = row.source_type === "pdf" && row.escalation.warning;

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="drawer-panel" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-header">
          <div className="drawer-title">
            <strong>{[row.vendor, row.product, row.name].join(" › ")}</strong>
            <div className="item-meta">
              {statusBadge(row.status)}
              <span className="kind-badge">{row.source_type}</span>
            </div>
          </div>
          <button className="overlay-close" title="Close" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="drawer-body">
          <button className="btn-secondary-sm" onClick={() => onOpenFull(row)}>
            Open full source view →
          </button>

          <section className="drawer-section">
            <h3>Recent runs</h3>
            {runsError && <div className="error">{runsError}</div>}
            {!runsError && runs === null && <p className="sub">Loading…</p>}
            {!runsError && runs !== null && runs.length === 0 && (
              <p className="sub">No runs yet.</p>
            )}
            {!runsError && runs !== null && runs.length > 0 && (
              <ul className="drawer-run-list">
                {runs.map((r) => (
                  <li key={r.id}>
                    {statusBadge(r.status)}
                    <span className="sub">
                      {r.articles_extracted ?? 0}n/{r.articles_updated ?? 0}u/
                      {r.articles_unchanged ?? 0}=
                    </span>
                    <span className="sub">
                      {r.started_at ? new Date(r.started_at).toLocaleString() : "—"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="drawer-section">
            <h3>Image enrichment</h3>
            <p className="sub">
              {row.enrichment.described.toLocaleString()} /{" "}
              {(row.enrichment.described + row.enrichment.pending).toLocaleString()} images
              described
            </p>
            {enrMsg && <p className="sub run-done">{enrMsg}</p>}
            <button
              className="btn-secondary-sm"
              title={row.active_run ? "A run is already active" : undefined}
              disabled={row.active_run || row.enrichment.pending === 0 || enriching}
              onClick={handleEnrich}
            >
              {enriching ? "Queuing…" : "Describe missing images"}
            </button>
          </section>

          {showEscalation && (
            <section className="drawer-section">
              <h3>PDF escalation</h3>
              <p className="sub">{row.escalation.pending_count} segments pending</p>
              {escMsg && <p className="sub run-done">{escMsg}</p>}
              <button
                className="btn-secondary-sm"
                title={row.active_run ? "A run is already active" : undefined}
                disabled={row.active_run || escalating}
                onClick={handleRetryEscalation}
              >
                {escalating ? "Queuing…" : "Retry escalation"}
              </button>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
