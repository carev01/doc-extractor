/**
 * Pure view logic for the Dashboard: flag derivation, filtering, and sorting.
 *
 * Deliberately free of React/DOM so it stays trivially correct and
 * unit-testable in isolation.
 */
import type { OverviewSourceRow } from "./types";

export type Flag =
  | "never"
  | "stale"
  | "failed"
  | "enrichment-backlog"
  | "escalation-warning"
  | "running";

export function rowFlags(row: OverviewSourceRow, staleSeconds: number): Flag[] {
  const flags: Flag[] = [];
  if (row.age_seconds === null) flags.push("never");
  if (row.age_seconds !== null && row.age_seconds > staleSeconds) flags.push("stale");
  if (row.status === "failed") flags.push("failed");
  if (row.enrichment.pending > 0) flags.push("enrichment-backlog");
  if (row.escalation.warning) flags.push("escalation-warning");
  if (row.active_run) flags.push("running");
  return flags;
}

export interface DashFilters {
  search: string;
  vendors: string[];
  types: string[];
  statuses: string[];
  flags: Flag[];
  tile: string | null;
}

export interface DashSort {
  key: string;
  dir: "asc" | "desc";
}

/** Maps a tile id to the flag it constrains the table to; null/unknown = no constraint. */
function tileFlag(tile: string | null): Flag | null {
  switch (tile) {
    case "stale":
      return "stale";
    case "failing":
      return "failed";
    case "running":
      return "running";
    case "enrichment":
      return "enrichment-backlog";
    case "escalation":
      return "escalation-warning";
    default:
      return null;
  }
}

// Surface problems first: never → failed → stale → escalation-warning → rest, then by name.
function attentionRank(flags: Flag[]): number {
  if (flags.includes("never")) return 0;
  if (flags.includes("failed")) return 1;
  if (flags.includes("stale")) return 2;
  if (flags.includes("escalation-warning")) return 3;
  return 4;
}

function sortKeyValue(row: OverviewSourceRow, key: string): string | number | null {
  switch (key) {
    case "name":
      return `${row.vendor}${row.product}${row.name}`.toLocaleLowerCase();
    case "freshness":
      return row.age_seconds;
    case "articles":
      return row.article_count;
    case "last_run":
      return row.last_run?.status ?? "";
    case "pending":
      return row.enrichment.pending;
    case "escalation":
      return row.escalation.pending_count;
    default:
      return null;
  }
}

export function filterAndSortRows(
  rows: OverviewSourceRow[],
  filters: DashFilters,
  sort: DashSort | null,
  staleSeconds: number,
): OverviewSourceRow[] {
  const search = filters.search.trim().toLowerCase();
  const requiredTileFlag = tileFlag(filters.tile);

  const filtered = rows.filter((row) => {
    if (search) {
      const haystack = `${row.vendor} ${row.product} ${row.name}`.toLowerCase();
      if (!haystack.includes(search)) return false;
    }
    if (filters.vendors.length > 0 && !filters.vendors.includes(row.vendor)) return false;
    if (filters.types.length > 0 && !filters.types.includes(row.source_type)) return false;
    if (filters.statuses.length > 0 && !filters.statuses.includes(row.status)) return false;

    const flags = rowFlags(row, staleSeconds);
    if (filters.flags.length > 0 && !filters.flags.every((f) => flags.includes(f))) return false;
    if (requiredTileFlag && !flags.includes(requiredTileFlag)) return false;

    return true;
  });

  const sorted = [...filtered];
  if (sort) {
    const dirMul = sort.dir === "asc" ? 1 : -1;
    sorted.sort((a, b) => {
      const va = sortKeyValue(a, sort.key);
      const vb = sortKeyValue(b, sort.key);
      // nulls first on asc (last on desc) — only relevant for freshness's age_seconds.
      if (va === null && vb === null) return 0;
      if (va === null) return -1 * dirMul;
      if (vb === null) return 1 * dirMul;
      if (typeof va === "string" && typeof vb === "string") {
        return va.localeCompare(vb) * dirMul;
      }
      if (va < vb) return -1 * dirMul;
      if (va > vb) return 1 * dirMul;
      return 0;
    });
  } else {
    sorted.sort((a, b) => {
      const ra = attentionRank(rowFlags(a, staleSeconds));
      const rb = attentionRank(rowFlags(b, staleSeconds));
      if (ra !== rb) return ra - rb;
      return `${a.vendor}${a.product}${a.name}`.localeCompare(`${b.vendor}${b.product}${b.name}`);
    });
  }

  return sorted;
}
