import { useEffect, useMemo, useState } from "react";
import type {
  DashboardOverview,
  DocumentationSource,
  OverviewSourceRow,
} from "../types";
import { getDashboardOverview, getSource, enrichSource } from "../api/client";
import { apiError } from "../api/errors";
import {
  filterAndSortRows,
  rowFlags,
  type DashFilters,
  type DashSort,
  type Flag,
} from "../dashboardView";
import DashboardDrawer from "./DashboardDrawer";

function fmtAge(seconds: number | null): string {
  if (seconds === null) return "never";
  const d = Math.floor(seconds / 86400);
  if (d >= 1) return `${d}d ago`;
  const h = Math.floor(seconds / 3600);
  if (h >= 1) return `${h}h ago`;
  const m = Math.floor(seconds / 60);
  return `${m}m ago`;
}

const FLAG_LABELS: Record<Flag, string> = {
  never: "Never extracted",
  stale: "Stale",
  failed: "Failed",
  "enrichment-backlog": "Enrichment backlog",
  "escalation-warning": "Escalation warning",
  running: "Running",
};

const ALL_FLAGS: Flag[] = [
  "never",
  "stale",
  "failed",
  "enrichment-backlog",
  "escalation-warning",
  "running",
];

const SORT_COLUMNS: { key: string; label: string }[] = [
  { key: "name", label: "Source" },
  { key: "freshness", label: "Last extracted" },
  { key: "articles", label: "Articles" },
  { key: "last_run", label: "Last run" },
  { key: "pending", label: "Img pending" },
  { key: "escalation", label: "Escalation" },
];

function FacetSelect({
  label,
  options,
  selected,
  onChange,
  renderOption,
}: {
  label: string;
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  renderOption?: (opt: string) => string;
}) {
  const toggle = (opt: string) => {
    onChange(
      selected.includes(opt) ? selected.filter((o) => o !== opt) : [...selected, opt],
    );
  };
  return (
    <details className="facet">
      <summary className="facet-summary">
        {label}
        {selected.length > 0 && <span className="facet-count">{selected.length}</span>}
      </summary>
      <div className="facet-menu">
        {options.length === 0 && <span className="sub">No options</span>}
        {options.map((opt) => (
          <label key={opt} className="facet-option">
            <input
              type="checkbox"
              checked={selected.includes(opt)}
              onChange={() => toggle(opt)}
            />
            {renderOption ? renderOption(opt) : opt}
          </label>
        ))}
      </div>
    </details>
  );
}

function FlagBadges({ row, staleSeconds }: { row: OverviewSourceRow; staleSeconds: number }) {
  const flags = rowFlags(row, staleSeconds);
  if (flags.length === 0) return null;
  return (
    <span className="flag-badges">
      {flags.includes("stale") && <span className="flag-badge flag-stale">stale</span>}
      {flags.includes("failed") && <span className="flag-badge flag-failed">failed</span>}
      {flags.includes("running") && (
        <span className="flag-badge flag-running" title="Extraction running">
          ▶
        </span>
      )}
    </span>
  );
}

export default function Dashboard({
  onSelectSource,
}: {
  onSelectSource: (s: DocumentationSource) => void;
}) {
  const [data, setData] = useState<DashboardOverview | null>(null);
  const [error, setError] = useState("");
  const [enrMsg, setEnrMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [enriching, setEnriching] = useState<string | null>(null);
  const staleSeconds = 30 * 86400;

  const [search, setSearch] = useState("");
  const [vendors, setVendors] = useState<string[]>([]);
  const [types, setTypes] = useState<string[]>([]);
  const [statuses, setStatuses] = useState<string[]>([]);
  const [flags, setFlags] = useState<Flag[]>([]);
  const [tile, setTile] = useState<string | null>(null);
  const [sort, setSort] = useState<DashSort | null>(null);
  const [selected, setSelected] = useState<OverviewSourceRow | null>(null);

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
    setEnrMsg(null);
    setEnriching(sourceId);
    try {
      await enrichSource(sourceId);
      setEnrMsg({ text: "Enrichment queued", ok: true });
      reload();
    } catch (e) {
      setEnrMsg({ text: apiError(e, "Failed to queue enrichment"), ok: false });
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

  const vendorOptions = useMemo(
    () =>
      data ? Array.from(new Set(data.sources.map((s) => s.vendor))).sort() : [],
    [data],
  );
  const typeOptions = useMemo(
    () =>
      data ? Array.from(new Set(data.sources.map((s) => s.source_type))).sort() : [],
    [data],
  );
  const statusOptions = useMemo(
    () => (data ? Array.from(new Set(data.sources.map((s) => s.status))).sort() : []),
    [data],
  );

  const filters: DashFilters = useMemo(
    () => ({ search, vendors, types, statuses, flags, tile }),
    [search, vendors, types, statuses, flags, tile],
  );

  const rows = useMemo(
    () => (data ? filterAndSortRows(data.sources, filters, sort, staleSeconds) : []),
    [data, filters, sort, staleSeconds],
  );

  const activeFilterCount =
    (search.trim() ? 1 : 0) +
    vendors.length +
    types.length +
    statuses.length +
    flags.length +
    (tile ? 1 : 0);

  const clearFilters = () => {
    setSearch("");
    setVendors([]);
    setTypes([]);
    setStatuses([]);
    setFlags([]);
    setTile(null);
    setSort(null);
  };

  const toggleTile = (id: string) => {
    if (id === "sources") {
      clearFilters();
      return;
    }
    setTile((t) => (t === id ? null : id));
  };

  const handleSort = (key: string) => {
    setSort((prev) => {
      if (!prev || prev.key !== key) return { key, dir: "asc" };
      return { key, dir: prev.dir === "asc" ? "desc" : "asc" };
    });
  };

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
  const tiles = [
    { id: "sources", label: "Sources", count: agg.total, cls: "" },
    { id: "never", label: "Never extracted", count: agg.never_extracted, cls: "warn" },
    { id: "stale", label: "Stale (>30d)", count: agg.stale, cls: "warn" },
    { id: "failing", label: "Failing", count: agg.failing, cls: "bad" },
    { id: "running", label: "Running", count: agg.running, cls: "" },
    {
      id: "enrichment",
      label: "Enrichment backlog",
      count: agg.enrichment.sources_with_backlog,
      cls: "warn",
    },
    {
      id: "escalation",
      label: "Escalation",
      count: agg.escalation_sources_with_warning,
      cls: "bad",
    },
  ];

  return (
    <div className="dashboard fade-in-up">
      <h2>Dashboard</h2>
      <div className="tile-row">
        {tiles.map((t) => (
          <div
            key={t.id}
            className={`tile clickable-tile ${t.cls} ${tile === t.id ? "active" : ""}`.trim()}
            onClick={() => toggleTile(t.id)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") toggleTile(t.id);
            }}
          >
            <span className="tile-n">{t.count}</span>
            <span className="tile-label">{t.label}</span>
          </div>
        ))}
      </div>

      <div className="filter-bar">
        <input
          type="text"
          className="filter-search"
          placeholder="Search vendor / product / name…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <FacetSelect label="Vendor" options={vendorOptions} selected={vendors} onChange={setVendors} />
        <FacetSelect label="Type" options={typeOptions} selected={types} onChange={setTypes} />
        <FacetSelect label="Status" options={statusOptions} selected={statuses} onChange={setStatuses} />
        <FacetSelect
          label="Flags"
          options={ALL_FLAGS}
          selected={flags}
          onChange={(next) => setFlags(next as Flag[])}
          renderOption={(f) => FLAG_LABELS[f as Flag]}
        />
        {activeFilterCount > 0 && (
          <button className="btn-secondary-sm filter-clear" onClick={clearFilters}>
            {activeFilterCount} filter{activeFilterCount === 1 ? "" : "s"} · Clear
          </button>
        )}
      </div>

      <table className="dashboard-table">
        <thead>
          <tr>
            {SORT_COLUMNS.map((c) => (
              <th key={c.key} className="sortable" onClick={() => handleSort(c.key)}>
                {c.label}
                {sort?.key === c.key ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
              </th>
            ))}
            <th>Status</th>
            <th>Job</th>
            <th>Flags</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} onClick={() => setSelected(r)} className="clickable-row">
              <td>{[r.vendor, r.product, r.name].join(" › ")}</td>
              <td>{fmtAge(r.age_seconds)}</td>
              <td>{r.article_count}</td>
              <td>
                {r.last_run
                  ? `${r.last_run.status} (${r.last_run.new ?? 0}n/${r.last_run.updated ?? 0}u/${r.last_run.unchanged ?? 0}=)`
                  : "—"}
              </td>
              <td className={r.enrichment.pending > 0 ? "cell-warn" : "sub"}>
                {r.enrichment.pending || "—"}
              </td>
              <td className={r.escalation.pending_count > 0 ? "cell-bad" : "sub"}>
                {r.escalation.pending_count || "—"}
              </td>
              <td>{r.status}</td>
              <td>{r.job_name ?? "—"}</td>
              <td>
                <FlagBadges row={r} staleSeconds={staleSeconds} />
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={9} className="sub">No sources match the current filters.</td></tr>
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
        {enrMsg && <p className={enrMsg.ok ? "sub run-done" : "error"}>{enrMsg.text}</p>}
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

      {selected && (
        <DashboardDrawer
          key={selected.id}
          // Prefer the freshly-polled row so post-action stats/gating update in
          // place; the stable key keeps the drawer (and its runs list) mounted.
          row={data.sources.find((s) => s.id === selected.id) ?? selected}
          onClose={() => setSelected(null)}
          onAction={reload}
          onOpenFull={(r) => {
            setSelected(null);
            openSource(r.id);
          }}
        />
      )}
    </div>
  );
}
