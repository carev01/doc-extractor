import { useEffect, useMemo, useState } from "react";
import type {
  DashboardOverview,
  DocumentationSource,
  OverviewSourceRow,
} from "../types";
import { getDashboardOverview, getSource, enrichSource } from "../api/client";
import { apiError } from "../api/errors";

function fmtAge(seconds: number | null): string {
  if (seconds === null) return "never";
  const d = Math.floor(seconds / 86400);
  if (d >= 1) return `${d}d ago`;
  const h = Math.floor(seconds / 3600);
  if (h >= 1) return `${h}h ago`;
  const m = Math.floor(seconds / 60);
  return `${m}m ago`;
}

// Surface problems first: never → failed → stale → rest, then by name.
function healthRank(r: OverviewSourceRow, staleSeconds: number): number {
  if (r.age_seconds === null) return 0;
  if (r.status === "failed") return 1;
  if (r.age_seconds > staleSeconds) return 2;
  return 3;
}

export default function Dashboard({
  onSelectSource,
}: {
  onSelectSource: (s: DocumentationSource) => void;
}) {
  const [data, setData] = useState<DashboardOverview | null>(null);
  const [error, setError] = useState("");
  const [enrMsg, setEnrMsg] = useState("");
  const [enriching, setEnriching] = useState<string | null>(null);
  const staleSeconds = 30 * 86400;

  const reload = () => {
    getDashboardOverview()
      .then(setData)
      .catch(() => setError("Failed to load dashboard"));
  };

  useEffect(() => {
    reload();
  }, []);

  // Keep the table/rollup/backlog fresh as enrich/extraction runs complete elsewhere.
  useEffect(() => {
    const id = setInterval(reload, 20000);
    return () => clearInterval(id);
  }, []);

  const handleEnrich = async (sourceId: string) => {
    setEnrMsg("");
    setEnriching(sourceId);
    try {
      await enrichSource(sourceId);
      setEnrMsg("Enrichment queued");
      reload();
    } catch (e) {
      setEnrMsg(apiError(e, "Failed to queue enrichment"));
    } finally {
      setEnriching(null);
    }
  };

  const backlog = useMemo(
    () =>
      data
        ? [...data.sources]
            .filter((s) => s.enrichment.pending > 0)
            .sort((a, b) => b.enrichment.pending - a.enrichment.pending)
        : [],
    [data],
  );

  const sorted = useMemo(() => {
    if (!data) return [];
    return [...data.sources].sort((a, b) => {
      const ra = healthRank(a, staleSeconds);
      const rb = healthRank(b, staleSeconds);
      if (ra !== rb) return ra - rb;
      return `${a.vendor}${a.product}${a.name}`.localeCompare(
        `${b.vendor}${b.product}${b.name}`,
      );
    });
  }, [data, staleSeconds]);

  const openSource = async (id: string) => {
    try {
      onSelectSource(await getSource(id));
    } catch {
      setError("Failed to open source");
    }
  };

  if (error) return <div className="error">{error}</div>;
  if (!data) return <p className="sub">Loading…</p>;

  const agg = data.aggregate;
  return (
    <div className="dashboard fade-in-up">
      <h2>Dashboard</h2>
      <div className="tile-row">
        <div className="tile">
          <span className="tile-n">{agg.total}</span>
          <span className="tile-label">Sources</span>
        </div>
        <div className="tile warn">
          <span className="tile-n">{agg.never_extracted}</span>
          <span className="tile-label">Never extracted</span>
        </div>
        <div className="tile warn">
          <span className="tile-n">{agg.stale}</span>
          <span className="tile-label">Stale (&gt;30d)</span>
        </div>
        <div className="tile bad">
          <span className="tile-n">{agg.failing}</span>
          <span className="tile-label">Failing</span>
        </div>
        <div className="tile">
          <span className="tile-n">{agg.running}</span>
          <span className="tile-label">Running</span>
        </div>
      </div>
      <table className="dashboard-table">
        <thead>
          <tr>
            <th>Source</th><th>Status</th><th>Last extracted</th>
            <th>Articles</th><th>Last run</th><th>Job</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={r.id} onClick={() => openSource(r.id)} className="clickable-row">
              <td>{[r.vendor, r.product, r.name].join(" › ")}</td>
              <td>{r.status}</td>
              <td>{fmtAge(r.age_seconds)}</td>
              <td>{r.article_count}</td>
              <td>
                {r.last_run
                  ? `${r.last_run.status} (${r.last_run.new ?? 0}n/${r.last_run.updated ?? 0}u/${r.last_run.unchanged ?? 0}=)`
                  : "—"}
              </td>
              <td>{r.job_name ?? "—"}</td>
            </tr>
          ))}
          {sorted.length === 0 && (
            <tr><td colSpan={6} className="sub">No sources yet.</td></tr>
          )}
        </tbody>
      </table>

      <div className="enrichment-section">
        <h3>Image enrichment</h3>
        <p className="sub">
          {agg.enrichment.described.toLocaleString()} /{" "}
          {(agg.enrichment.described + agg.enrichment.pending).toLocaleString()} images described
          {" · "}
          {agg.enrichment.sources_with_backlog} source
          {agg.enrichment.sources_with_backlog === 1 ? "" : "s"} with a backlog
        </p>
        {enrMsg && <p className="sub run-done">{enrMsg}</p>}
        {backlog.length === 0 ? (
          <p className="sub">All images described.</p>
        ) : (
          <table className="dashboard-table">
            <thead>
              <tr><th>Source</th><th>Pending</th><th></th></tr>
            </thead>
            <tbody>
              {backlog.map((row) => (
                <tr key={row.id}>
                  <td>{[row.vendor, row.product, row.name].join(" › ")}</td>
                  <td>{row.enrichment.pending}</td>
                  <td>
                    <button
                      className="btn-secondary-sm"
                      title={row.active_run ? "A run is already active" : undefined}
                      disabled={row.active_run || enriching === row.id}
                      onClick={() => handleEnrich(row.id)}
                    >
                      {enriching === row.id ? "Queuing…" : "Enrich"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
