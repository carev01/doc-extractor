import { useEffect, useMemo, useState } from "react";
import type { ScheduleJobRef, ScheduleLatest, ScheduleProduct } from "../types";
import { getSchedule } from "../api/client";
import { apiError } from "../api/errors";

/** "Nightly + Weekly (disabled)" — a disabled job covers the row but never fires. */
function jobsLabel(jobs: ScheduleJobRef[]): string {
  return jobs.map((j) => (j.enabled ? j.name : `${j.name} (disabled)`)).join(" + ");
}

const REASONS: Record<string, string> = {
  active_run: "a run was already active",
  paused_run: "a paused run was in the way",
  login_needed: "login needed",
  login_failed: "login failed",
};

function fmtDate(iso: string | null): string {
  if (!iso) return "never";
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/** Calendar days in the viewer's local time, so 23:00 tonight vs 01:00 tomorrow is "in 1 day". */
function inDays(iso: string): string {
  const at = new Date(iso);
  if (at.getTime() < Date.now()) return "overdue";
  const midnight = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((midnight(at) - midnight(new Date())) / 86400000);
  if (days <= 0) return "today";
  return days === 1 ? "in 1 day" : `in ${days} days`;
}

const TRIGGERS: Record<string, string> = {
  scheduled: "job",
  manual: "manual",
  bulk: "bulk",
  force: "force",
  "version-bump": "version bump",
  version_bump: "version bump",
};

function triggerLabel(l: ScheduleLatest): string {
  const t = l.trigger ? (TRIGGERS[l.trigger] ?? l.trigger) : "";
  if (l.job_name) return `${t || "job"}: ${l.job_name}`;
  return t;
}

function Outcome({ l }: { l: ScheduleLatest }) {
  const reason = l.reason ? (REASONS[l.reason] ?? l.reason) : "";
  let icon = "✓";
  let color = "var(--new)";
  let text: string;
  switch (l.outcome) {
    case "ok":
      text = l.updated > 0 ? `${l.updated} updated` : "unchanged";
      break;
    case "warn":
      icon = "⚠";
      color = "var(--amber)";
      text = l.blocked > 0 ? `${l.blocked} blocked page${l.blocked === 1 ? "" : "s"}` : "completed with warnings";
      break;
    case "failed":
      icon = "⚠";
      color = "var(--gone)";
      text = reason ? `failed: ${reason}` : "failed";
      break;
    default:
      icon = "⚠";
      color = "var(--amber)";
      text = `skipped${reason ? `: ${reason}` : ""}`;
  }
  return (
    <span
      style={{
        color,
        display: "-webkit-box",
        WebkitLineClamp: 2,
        WebkitBoxOrient: "vertical",
        overflow: "hidden",
        overflowWrap: "anywhere",
      }}
      title={l.reason ?? undefined}
    >
      {icon} {text}
    </span>
  );
}

function LastCell({ latest, lastOk }: { latest: ScheduleLatest | null; lastOk: string | null }) {
  if (!latest) return <span className="muted">never refreshed</span>;
  return (
    <>
      <div>
        {fmtDate(latest.at)} <Outcome l={latest} />
      </div>
      {triggerLabel(latest) && <div className="hint">{triggerLabel(latest)}</div>}
      {(latest.outcome !== "ok" || !lastOk || new Date(lastOk) < new Date(latest.at)) && (
        <div className="hint">last OK: {lastOk ? fmtDate(lastOk) : "never"}</div>
      )}
    </>
  );
}

function NextCell({ nextAt, scheduled, total }: { nextAt: string | null; scheduled?: number; total?: number }) {
  if (!nextAt) return <span className="muted">not scheduled</span>;
  return (
    <>
      <div>
        {fmtDate(nextAt)} <span className="hint">{inDays(nextAt)}</span>
      </div>
      {scheduled !== undefined && total !== undefined && scheduled < total && (
        <div className="hint">partly scheduled: {scheduled} of {total} sources</div>
      )}
    </>
  );
}

export default function ScheduleView() {
  const [products, setProducts] = useState<ScheduleProduct[] | null>(null);
  const [error, setError] = useState("");
  const [text, setText] = useState("");
  const [tier, setTier] = useState("all");
  const [open, setOpen] = useState<Set<string>>(new Set());

  const [reloads, setReloads] = useState(0);
  const [refreshing, setRefreshing] = useState(true);

  useEffect(() => {
    let live = true;
    getSchedule()
      .then((r) => {
        if (!live) return;
        setProducts(r.products);
        setError("");
      })
      .catch((e) => live && setError(apiError(e, "Failed to load schedule")))
      .finally(() => live && setRefreshing(false));
    return () => {
      live = false;
    };
  }, [reloads]);

  const refresh = () => {
    setRefreshing(true);
    setReloads((n) => n + 1);
  };

  const tiers = useMemo(
    () => [...new Set((products ?? []).map((p) => p.tier_name).filter((t): t is string => !!t))].sort(),
    [products],
  );

  const shown = useMemo(() => {
    const q = text.trim().toLowerCase();
    return (products ?? []).filter((p) => {
      if (tier === "none" ? p.tier_name : tier !== "all" && p.tier_name !== tier) return false;
      return !q || `${p.vendor_name} ${p.product_name}`.toLowerCase().includes(q);
    });
  }, [products, text, tier]);

  const toggle = (id: string) =>
    setOpen((prev) => {
      const n = new Set(prev);
      if (!n.delete(id)) n.add(id);
      return n;
    });

  if (error && !products)
    return (
      <div>
        <div className="error">{error}</div>
        <button type="button" className="btn-secondary-sm" onClick={refresh}>
          Retry
        </button>
      </div>
    );
  if (!products) return <p className="muted">Loading…</p>;

  return (
    <section className="jobs-section">
      <div style={{ display: "flex", gap: "0.5rem", marginBottom: "0.75rem", flexWrap: "wrap" }}>
        <input
          type="search"
          placeholder="Filter vendor / product"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <select value={tier} onChange={(e) => setTier(e.target.value)}>
          <option value="all">All tiers</option>
          {tiers.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
          <option value="none">No tier</option>
        </select>
        <button type="button" className="btn-secondary-sm" disabled={refreshing} onClick={refresh}>
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
        {error && <span className="error-inline">{error}</span>}
      </div>
      {shown.length === 0 && <p className="empty">No products match.</p>}
      {shown.length > 0 && (
        <table className="dashboard-table">
          <thead>
            <tr>
              <th>Product</th>
              <th>Last refreshed</th>
              <th>Next</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((p) => {
              const isOpen = open.has(p.product_id);
              const empty = p.source_count === 0;
              return (
                <ProductRows key={p.product_id} p={p} isOpen={isOpen} empty={empty} onToggle={() => toggle(p.product_id)} />
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

function ProductRows({
  p,
  isOpen,
  empty,
  onToggle,
}: {
  p: ScheduleProduct;
  isOpen: boolean;
  empty: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr style={{ cursor: empty ? "default" : "pointer" }} onClick={empty ? undefined : onToggle}>
        <td>
          {empty ? (
            <strong>{p.vendor_name} › {p.product_name}</strong>
          ) : (
            <button
              type="button"
              className="link-btn"
              style={{ font: "inherit", color: "inherit", padding: 0, textAlign: "left", cursor: "pointer" }}
              aria-expanded={isOpen}
              onClick={(e) => {
                e.stopPropagation();
                onToggle();
              }}
            >
              <span aria-hidden>{isOpen ? "▾ " : "▸ "}</span>
              <strong>{p.vendor_name} › {p.product_name}</strong>
            </button>
          )}
          <div className="hint">
            {p.tier_name ?? "no tier"}
            {p.jobs.length > 0 ? ` · ${jobsLabel(p.jobs)}` : ""}
          </div>
        </td>
        {empty ? (
          <td colSpan={2} className="muted">no sources</td>
        ) : (
          <>
            <td><LastCell latest={p.latest} lastOk={p.last_ok_at} /></td>
            <td><NextCell nextAt={p.next_at} scheduled={p.scheduled_sources} total={p.source_count} /></td>
          </>
        )}
      </tr>
      {isOpen &&
        p.sources.map((s) => (
          <tr key={s.source_id} style={{ background: "var(--ink-2)" }}>
            <td style={{ paddingLeft: "2rem" }}>
              {s.source_name}
              {s.jobs.length > 0 && <div className="hint">{jobsLabel(s.jobs)}</div>}
            </td>
            <td><LastCell latest={s.latest} lastOk={s.last_ok_at} /></td>
            <td><NextCell nextAt={s.next_at} /></td>
          </tr>
        ))}
    </>
  );
}
