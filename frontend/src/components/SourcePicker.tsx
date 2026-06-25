import { useEffect, useMemo, useState } from "react";
import type { PickableSource } from "../types";
import { listPickableSources, assignSourcesToJob } from "../api/client";

export default function SourcePicker({
  jobId,
  onClose,
  onAssigned,
}: {
  jobId: string;
  onClose: () => void;
  onAssigned: () => void;
}) {
  const [sources, setSources] = useState<PickableSource[]>([]);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    listPickableSources()
      .then(setSources)
      .catch(() => setError("Failed to load sources"));
  }, []);

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return sources;
    return sources.filter((s) =>
      `${s.vendor_name} ${s.product_name} ${s.name}`.toLowerCase().includes(q),
    );
  }, [sources, filter]);

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const assign = async () => {
    if (selected.size === 0) return;
    setSaving(true);
    setError("");
    try {
      await assignSourcesToJob(jobId, [...selected]);
      onAssigned();
      onClose();
    } catch {
      setError("Failed to assign sources");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="picker-backdrop" onClick={onClose}>
      <div className="picker-panel" onClick={(e) => e.stopPropagation()}>
        <h3>Add sources</h3>
        {error && <div className="error">{error}</div>}
        <input
          type="text"
          placeholder="Filter by vendor, product or source…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <ul className="picker-list">
          {visible.map((s) => (
            <li key={s.id}>
              <label>
                <input
                  type="checkbox"
                  checked={selected.has(s.id)}
                  onChange={() => toggle(s.id)}
                />
                <span>{[s.vendor_name, s.product_name, s.name].join(" › ")}</span>
                {s.job_id && s.job_id !== jobId && (
                  <span className="sub"> (in: {s.job_name})</span>
                )}
                {s.job_id === jobId && <span className="sub"> (already here)</span>}
              </label>
            </li>
          ))}
          {visible.length === 0 && <li className="sub">No sources match.</li>}
        </ul>
        <div className="picker-actions">
          <button className="btn-secondary-sm" onClick={onClose}>Cancel</button>
          <button
            className="btn-primary-sm"
            disabled={saving || selected.size === 0}
            onClick={assign}
          >
            {saving ? "Assigning…" : `Assign ${selected.size || ""}`.trim()}
          </button>
        </div>
      </div>
    </div>
  );
}
