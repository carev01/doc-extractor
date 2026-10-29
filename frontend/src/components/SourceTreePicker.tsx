import { useEffect, useRef, useState } from "react";

/** One pickable source. A ``disabled`` source cannot be ticked; ``note`` says why. */
export interface TreeSource {
  id: string;
  name: string;
  disabled?: boolean;
  note?: string;
}

export interface TreeProduct {
  id: string;
  name: string;
  sources: TreeSource[];
}

/** An optional top level: a vendor's products. ``note`` shows beside its name. */
export interface TreeVendor {
  id: string;
  name: string;
  products: TreeProduct[];
  note?: string;
}

interface Props {
  /** Product rows at the top level (bulk extract). Ignored when ``vendors`` is given. */
  products?: TreeProduct[];
  /** Vendor rows at the top level, each holding its products (job coverage). */
  vendors?: TreeVendor[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
  /** Hide the product rows (a single product's own sources). */
  flat?: boolean;
}

/** A group's checkbox: ticked when all its enabled sources are, a dash when some are. */
function GroupBox({
  all,
  some,
  disabled,
  onToggle,
  label,
}: {
  all: boolean;
  some: boolean;
  disabled: boolean;
  onToggle: () => void;
  label: string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = some && !all;
  }, [some, all]);
  return (
    <input
      ref={ref}
      type="checkbox"
      aria-label={label}
      checked={all}
      disabled={disabled}
      onChange={onToggle}
    />
  );
}

/**
 * Vendor → product → source checkbox tree. Controlled: the caller owns the
 * selected source ids, so a dialog (bulk extract) or a form (a job's coverage)
 * can each decide what starts ticked.
 */
export default function SourceTreePicker({
  products = [],
  vendors,
  selected,
  onChange,
  flat,
}: Props) {
  // Vendors and products start collapsed: a vendor can have dozens of sources,
  // and a group's checkbox already selects or clears all of them without expanding.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const setMany = (ids: string[], on: boolean) => {
    const next = new Set(selected);
    for (const id of ids) {
      if (on) next.add(id);
      else next.delete(id);
    }
    onChange(next);
  };

  const toggleExpanded = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const sourceRow = (s: TreeSource) => (
    <li key={s.id} className="tree-row">
      <label className="chapter-toggle">
        <input
          type="checkbox"
          disabled={s.disabled}
          checked={!s.disabled && selected.has(s.id)}
          onChange={() => setMany([s.id], !selected.has(s.id))}
        />
        <span className={s.disabled ? "muted" : undefined}>{s.name}</span>
        {s.note && <span className="hint"> — {s.note}</span>}
      </label>
    </li>
  );

  const enabledIds = (ps: TreeProduct[]) =>
    ps.flatMap((p) => p.sources.filter((s) => !s.disabled).map((s) => s.id));

  /** Checkbox + caret + "k of n" header shared by vendor and product rows. */
  const groupHead = (
    id: string,
    name: string,
    enabled: string[],
    total: number,
    note?: string,
  ) => {
    const ticked = enabled.filter((sid) => selected.has(sid)).length;
    const all = enabled.length > 0 && ticked === enabled.length;
    const open = expanded.has(id);
    return (
      <div className="tree-product-head">
        <GroupBox
          all={all}
          some={ticked > 0}
          disabled={enabled.length === 0}
          onToggle={() => setMany(enabled, !all)}
          label={`Select all sources of ${name}`}
        />
        <button
          type="button"
          className="tree-caret"
          aria-expanded={open}
          onClick={() => toggleExpanded(id)}
        >
          <span aria-hidden="true">{open ? "▾" : "▸"}</span>
          <span className="bump-plan-name">{name}</span>
          {note && <span className="hint tree-note">{note}</span>}
          <span className="hint">
            {ticked} of {total} source(s)
          </span>
        </button>
      </div>
    );
  };

  const productRow = (p: TreeProduct, nested: boolean) => (
    <li key={p.id} className={nested ? "tree-row tree-product" : "bump-plan-row tree-product"}>
      {groupHead(p.id, p.name, enabledIds([p]), p.sources.length)}
      {expanded.has(p.id) && <ul className="tree-sources">{p.sources.map(sourceRow)}</ul>}
    </li>
  );

  if (vendors) {
    return (
      <ul className="bump-plan">
        {vendors.map((v) => (
          <li key={v.id} className="bump-plan-row tree-product">
            {groupHead(
              v.id,
              v.name,
              enabledIds(v.products),
              v.products.reduce((n, p) => n + p.sources.length, 0),
              v.note,
            )}
            {expanded.has(v.id) && (
              <ul className="tree-sources">{v.products.map((p) => productRow(p, true))}</ul>
            )}
          </li>
        ))}
      </ul>
    );
  }

  if (flat) {
    return (
      <ul className="bump-plan tree-flat">{products.flatMap((p) => p.sources.map(sourceRow))}</ul>
    );
  }

  return <ul className="bump-plan">{products.map((p) => productRow(p, false))}</ul>;
}
