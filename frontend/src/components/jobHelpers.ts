/** Pure helpers shared by the jobs list and the job editor. */
import type { CatalogVendor, Job, JobSchedule, JobTargets } from "../types";

export const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
export const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
/** Month intervals the backend accepts: each divides a year evenly. */
export const INTERVALS = [1, 2, 3, 4, 6, 12];

/** "Every 3 months (from January) on the first Saturday at 02:00 Europe/Lisbon". */
export function scheduleSummary(s: JobSchedule): string {
  const at = `at ${s.time_of_day ?? "02:00"} ${s.timezone}`;
  const day = WEEKDAYS[s.weekday ?? 0];
  const every = () => {
    const n = s.interval_months ?? 1;
    if (n === 1) return "Every month";
    const from = s.start_month ? ` (from ${MONTHS[s.start_month - 1]})` : "";
    return `Every ${n} months${from}`;
  };
  switch (s.schedule_kind) {
    case "daily":
      return `Daily ${at}`;
    case "weekly":
      return `Weekly on ${day} ${at}`;
    case "monthly_day":
      return `${every()} on day ${s.day_of_month ?? 1} ${at}`;
    case "monthly_nth":
      return `${every()} on the ${s.nth ?? "first"} ${day} ${at}`;
    default:
      return "No schedule (manual only)";
  }
}

/** A run time shown in the job's own timezone; falls back to local time when
 * the zone is not one this browser knows. */
export function fmtInTz(iso: string, tz: string): string {
  const opts: Intl.DateTimeFormatOptions = {
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  };
  try {
    return new Date(iso).toLocaleString(undefined, { ...opts, timeZone: tz });
  } catch {
    return new Date(iso).toLocaleString(undefined, opts);
  }
}

/** Indexes over the catalog the coverage maths needs. */
export interface CatalogIndex {
  /** source id → product id */
  productOf: Map<string, string>;
  /** source ids behind an auth realm */
  login: Set<string>;
  /** vendor id / product id → its source ids */
  sourcesOf: Map<string, string[]>;
  /** tier id → source ids of every vendor holding it */
  tierSources: Map<string, string[]>;
  /** tier id → vendor id → that vendor's source ids */
  tierVendorSources: Map<string, Map<string, string[]>>;
}

export function indexCatalog(catalog: CatalogVendor[]): CatalogIndex {
  const idx: CatalogIndex = {
    productOf: new Map(),
    login: new Set(),
    sourcesOf: new Map(),
    tierSources: new Map(),
    tierVendorSources: new Map(),
  };
  for (const v of catalog) {
    const vs: string[] = [];
    for (const p of v.products) {
      const ps = p.sources.map((s) => s.source_id);
      idx.sourcesOf.set(p.product_id, ps);
      vs.push(...ps);
      for (const s of p.sources) {
        idx.productOf.set(s.source_id, p.product_id);
        if (s.auth_realm) idx.login.add(s.source_id);
      }
    }
    idx.sourcesOf.set(v.vendor_id, vs);
    if (v.tier_id) {
      idx.tierSources.set(v.tier_id, [...(idx.tierSources.get(v.tier_id) ?? []), ...vs]);
      const byVendor = idx.tierVendorSources.get(v.tier_id) ?? new Map<string, string[]>();
      byVendor.set(v.vendor_id, vs);
      idx.tierVendorSources.set(v.tier_id, byVendor);
    }
  }
  return idx;
}

/** The explicit picks as concrete source ids (dynamic targets expanded). */
export function resolveTargets(t: JobTargets, idx: CatalogIndex): Set<string> {
  const out = new Set(t.source_ids);
  for (const id of [...t.vendor_ids, ...t.product_ids]) {
    for (const s of idx.sourcesOf.get(id) ?? []) out.add(s);
  }
  return out;
}

/** Everything a job covers today: its tier's sources (minus the excluded
 * vendors', tier part only) plus its explicit picks, which always win. */
export function resolveJob(
  tierId: string | null,
  picks: Set<string>,
  idx: CatalogIndex,
  excludedVendorIds: Iterable<string> = [],
): Set<string> {
  const out = new Set(picks);
  if (!tierId) return out;
  const excluded = new Set(excludedVendorIds);
  if (excluded.size === 0) {
    for (const s of idx.tierSources.get(tierId) ?? []) out.add(s);
    return out;
  }
  for (const [vendorId, sources] of idx.tierVendorSources.get(tierId) ?? []) {
    if (excluded.has(vendorId)) continue;
    for (const s of sources) out.add(s);
  }
  return out;
}

/** "N also covered by A, B" — the other jobs sharing any of these sources. */
export function overlapWith(
  covered: Set<string>,
  self: string | null,
  jobs: Job[],
  idx: CatalogIndex,
): { count: number; names: string[] } {
  const shared = new Set<string>();
  const names: string[] = [];
  for (const j of jobs) {
    if (j.id === self) continue;
    const theirs = resolveJob(j.tier_id, resolveTargets(j.targets, idx), idx, j.excluded_vendor_ids);
    let hit = false;
    for (const s of covered) {
      if (theirs.has(s)) {
        shared.add(s);
        hit = true;
      }
    }
    if (hit) names.push(j.name);
  }
  return { count: shared.size, names };
}

/** "covers 14 products / 62 sources; 2 also covered by Weekly; 3 need a login". */
export function coverageLine(
  c: {
    sources: number;
    products: number;
    overlap_sources: number;
    login_sources: number;
    excluded_vendors?: number;
  },
  overlapNames: string[] = [],
): string {
  const parts = [`covers ${c.products} product(s) / ${c.sources} source(s)`];
  if (c.overlap_sources > 0) {
    parts.push(
      `${c.overlap_sources} also covered by ${overlapNames.length ? overlapNames.join(", ") : "another job"}`,
    );
  }
  if (c.login_sources > 0) parts.push(`${c.login_sources} need a login`);
  const line = parts.join("; ");
  const ex = c.excluded_vendors ?? 0;
  return ex > 0 ? `${line} · ${ex} vendor${ex === 1 ? "" : "s"} excluded` : line;
}
