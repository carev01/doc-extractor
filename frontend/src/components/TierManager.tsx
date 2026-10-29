import { useState, useEffect, useCallback } from "react";
import axios from "axios";
import type { Tier } from "../types";
import { listTiers, createTier, updateTier, deleteTier } from "../api/client";
import { apiError } from "../api/errors";

/** Admin-only tier CRUD. Reorder swaps `sort_order` with the neighbour. */
export default function TierManager() {
  const [tiers, setTiers] = useState<Tier[]>([]);
  const [name, setName] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async (keepError = false) => {
    try {
      setTiers(await listTiers());
    } catch (e) {
      if (!keepError) setError(apiError(e, "Failed to load tiers"));
    }
  }, []);

  useEffect(() => {
    listTiers()
      .then(setTiers)
      .catch((e) => setError(apiError(e, "Failed to load tiers")));
  }, []);

  const run = async (fn: () => Promise<unknown>, fallback: string) => {
    setError("");
    let failed = false;
    try {
      await fn();
    } catch (e) {
      failed = true;
      setError(apiError(e, fallback));
    } finally {
      // Also after a failure: a reorder that died midway has already
      // renumbered some tiers, and the list on screen would be stale.
      await refresh(failed);
    }
  };

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    const next = tiers.length ? Math.max(...tiers.map((t) => t.sort_order)) + 1 : 0;
    run(async () => {
      await createTier({ name: name.trim(), sort_order: next });
      setName("");
    }, "Failed to create tier");
  };

  const handleRename = (t: Tier) => {
    const next = prompt("Rename tier", t.name);
    if (next === null || !next.trim() || next.trim() === t.name) return;
    run(() => updateTier(t.id, { name: next.trim() }), "Failed to rename tier");
  };

  const move = (idx: number, dir: -1 | 1) => {
    const a = tiers[idx];
    const b = tiers[idx + dir];
    if (!a || !b) return;
    // Equal sort_orders would make a swap a no-op: renumber the whole list.
    const order = tiers.map((t) => t.id);
    order[idx] = b.id;
    order[idx + dir] = a.id;
    run(async () => {
      for (let i = 0; i < order.length; i++) {
        const t = tiers.find((x) => x.id === order[i])!;
        if (t.sort_order !== i) await updateTier(t.id, { sort_order: i });
      }
    }, "Failed to reorder tiers");
  };

  const handleDelete = async (t: Tier) => {
    if (!confirm(`Delete tier "${t.name}"?`)) return;
    setError("");
    try {
      await deleteTier(t.id);
      await refresh();
    } catch (e) {
      const d = axios.isAxiosError(e) ? e.response?.data : undefined;
      if (axios.isAxiosError(e) && e.response?.status === 409 && d && typeof d.vendors === "number") {
        const names = (label: string, list: unknown, count: number) => {
          if (!Array.isArray(list) || list.length === 0) return "";
          const more = count > list.length ? ` and ${count - list.length} more` : "";
          return `${label}: ${list.join(", ")}${more}`;
        };
        const detail = [names("Vendors", d.vendor_names, d.vendors), names("Jobs", d.job_names, d.jobs)]
          .filter(Boolean)
          .join(" · ");
        setError(
          `${d.detail ?? "Tier in use"} (${d.vendors} vendor(s), ${d.jobs} job(s))` + (detail ? ` — ${detail}` : ""),
        );
      } else {
        setError(apiError(e, "Failed to delete tier"));
      }
    }
  };

  return (
    <section className="jobs-section">
      <div className="product-heading">
        <h3>Tiers</h3>
      </div>
      {error && <div className="error">{error}</div>}
      <form onSubmit={handleCreate} className="add-form">
        <input
          type="text"
          placeholder="New tier name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
        <button type="submit" className="btn-primary">Add Tier</button>
      </form>
      <ul className="item-list">
        {tiers.map((t, i) => (
          <li key={t.id}>
            <div className="item-info">
              <strong>{t.name}</strong>
              <span className="sub">{t.vendor_count} vendor(s) · {t.job_count} job(s)</span>
            </div>
            <div className="item-actions">
              <button className="btn-secondary-sm" disabled={i === 0} onClick={() => move(i, -1)} title="Move up">↑</button>
              <button className="btn-secondary-sm" disabled={i === tiers.length - 1} onClick={() => move(i, 1)} title="Move down">↓</button>
              <button className="btn-secondary-sm" title="Rename" onClick={() => handleRename(t)}>✎</button>
              <button className="btn-danger-sm" onClick={() => handleDelete(t)}>×</button>
            </div>
          </li>
        ))}
        {tiers.length === 0 && <li className="empty">No tiers yet.</li>}
      </ul>
    </section>
  );
}
