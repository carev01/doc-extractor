import type { BrowseTOCEntry } from "../types";

export interface FlatRow {
  node: BrowseTOCEntry;
  depth: number;
  hasChildren: boolean;
  expanded: boolean;
}

/** Depth-first flatten of the visible tree, skipping children of collapsed nodes. */
export function flattenVisible(
  entries: BrowseTOCEntry[],
  collapsed: Set<string>,
): FlatRow[] {
  const rows: FlatRow[] = [];
  const walk = (nodes: BrowseTOCEntry[], depth: number) => {
    for (const n of nodes) {
      const hasChildren = n.children.length > 0;
      const expanded = hasChildren && !collapsed.has(n.id);
      rows.push({ node: n, depth, hasChildren, expanded });
      if (expanded) walk(n.children, depth + 1);
    }
  };
  walk(entries, 0);
  return rows;
}

/**
 * Filtered view: include a node if its title matches (case-insensitive substring)
 * or any descendant matches; ancestors of a match are included and shown expanded.
 * Returns rows in depth-first order. Empty/blank query returns [].
 */
export function filterVisible(
  entries: BrowseTOCEntry[],
  query: string,
): FlatRow[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const rows: FlatRow[] = [];
  const subtree = (n: BrowseTOCEntry, depth: number, out: FlatRow[]): boolean => {
    const selfMatch = n.title.toLowerCase().includes(q);
    const childRows: FlatRow[] = [];
    let anyChild = false;
    for (const c of n.children) {
      anyChild = subtree(c, depth + 1, childRows) || anyChild;
    }
    if (selfMatch || anyChild) {
      out.push({
        node: n,
        depth,
        hasChildren: n.children.length > 0,
        expanded: anyChild, // ancestor of a match is shown expanded
      });
      out.push(...childRows);
      return true;
    }
    return false;
  };
  for (const e of entries) subtree(e, 0, rows);
  return rows;
}
