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

interface Props {
  products: TreeProduct[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
  /** Hide the product rows (a single product's own sources). */
  flat?: boolean;
}

/** A product's checkbox: ticked when all its enabled sources are, a dash when some are. */
function ProductBox({
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
export default function SourceTreePicker({ products, selected, onChange, flat }: Props) {
  // Products start collapsed: a vendor can have dozens of sources, and the
  // product checkbox already selects or clears all of them without expanding.
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

  if (flat) {
    return (
      <ul className="bump-plan tree-flat">{products.flatMap((p) => p.sources.map(sourceRow))}</ul>
    );
  }

  return (
    <ul className="bump-plan">
      {products.map((p) => {
        const enabled = p.sources.filter((s) => !s.disabled).map((s) => s.id);
        const ticked = enabled.filter((id) => selected.has(id)).length;
        const all = enabled.length > 0 && ticked === enabled.length;
        const open = expanded.has(p.id);
        return (
          <li key={p.id} className="bump-plan-row tree-product">
            <div className="tree-product-head">
              <ProductBox
                all={all}
                some={ticked > 0}
                disabled={enabled.length === 0}
                onToggle={() => setMany(enabled, !all)}
                label={`Select all sources of ${p.name}`}
              />
              <button
                type="button"
                className="tree-caret"
                aria-expanded={open}
                onClick={() => toggleExpanded(p.id)}
              >
                <span aria-hidden="true">{open ? "▾" : "▸"}</span>
                <span className="bump-plan-name">{p.name}</span>
                <span className="hint">
                  {ticked} of {p.sources.length} source(s)
                </span>
              </button>
            </div>
            {open && <ul className="tree-sources">{p.sources.map(sourceRow)}</ul>}
          </li>
        );
      })}
    </ul>
  );
}
