import { useEffect, useMemo, useState } from "react";
import type { ScheduleLatest, ScheduleProduct } from "../types";
import { getSchedule } from "../api/client";
import { apiError } from "../api/errors";

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

function inDays(iso: string): string {
  const days = Math.round((new Date(iso).getTime() - Date.now()) / 86400000);
  if (days <= 0) return "today";
  return days === 1 ? "in 1 day" : `in ${days} days`;
}

function triggerLabel(l: ScheduleLatest): string {
  if (l.job_name) return `job: ${l.job_name}`;
  return l.trigger ?? "";
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
    <span style={{ color }} title={l.reason ?? undefined}>
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
      <div className="hint">{triggerLabel(latest)}</div>
      {latest.outcome !== "ok" && (
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

  useEffect(() => {
    let live = true;
    getSchedule()
      .then((r) => live && setProducts(r.products))
      .catch((e) => live && setError(apiError(e, "Failed to load schedule")));
    return () => {
      live = false;
    };
  }, []);

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

  if (error) return <div className="error">{error}</div>;
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
          {!empty && <span aria-hidden>{isOpen ? "▾ " : "▸ "}</span>}
          <strong>{p.vendor_name} › {p.product_name}</strong>
          <div className="hint">
            {p.tier_name ?? "no tier"}
            {p.jobs.length > 0 ? ` · ${p.jobs.join(" + ")}` : ""}
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
              {s.jobs.length > 0 && <div className="hint">{s.jobs.join(" + ")}</div>}
            </td>
            <td><LastCell latest={s.latest} lastOk={s.last_ok_at} /></td>
            <td><NextCell nextAt={s.next_at} /></td>
          </tr>
        ))}
    </>
  );
}
