import { useState, useEffect, useCallback } from "react";
import type { Vendor, Tier } from "../types";
import { listAllVendors, createVendor, updateVendor, deleteVendor, listTiers, setVendorsTier } from "../api/client";
import { apiError } from "../api/errors";
import BulkImport from "./BulkImport";
import type { Access } from "../access";

interface Props {
  onSelect: (vendor: Vendor) => void;
  selectedId?: string;
  access: Access;
}

export default function VendorList({ onSelect, selectedId, access }: Props) {
  // Vendor create/rename/delete and the CSV import are all admin-only on the
  // server (require_admin), so a non-admin got controls that could only 403.
  // Rows stay clickable for everyone — that is how you reach the doc viewer.
  const canManage = access.canManageVendors;
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [name, setName] = useState("");
  const [website, setWebsite] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [showImport, setShowImport] = useState(false);
  // Tiers are admin-only on the server (/api/tiers), so only admins load them.
  const isAdmin = access.isAdmin;
  const [tiers, setTiers] = useState<Tier[]>([]);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [tierMenu, setTierMenu] = useState(false);

  useEffect(() => {
    if (!isAdmin) return;
    listTiers()
      .then(setTiers)
      .catch(() => setTiers([]));
  }, [isAdmin]);

  const toggleChecked = (id: string) =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const applyTier = async (tierId: string | null) => {
    setTierMenu(false);
    try {
      await setVendorsTier(tierId, [...checked]);
      setChecked(new Set());
      await fetchVendors();
      setTiers(await listTiers());
    } catch (e) {
      setError(apiError(e, "Failed to set tier"));
    }
  };
  const tierName = (id: string | null) => tiers.find((t) => t.id === id)?.name;

  // listAllVendors pages; a plain listVendors() call defaults to limit=50 and
  // would silently hide the 51st vendor onward — the same partial-list trap the
  // admin grant editor hit, and indistinguishable here from "not created yet".
  const fetchVendors = useCallback(async () => {
    try {
      setVendors(await listAllVendors());
    } catch {
      setError("Failed to load vendors");
    }
  }, []);

  // Inline promise chain rather than calling fetchVendors(): the
  // react-hooks/set-state-in-effect rule rejects invoking a function that
  // setStates from an effect body. Same shape as ProductList/SourceList.
  useEffect(() => {
    listAllVendors()
      .then(setVendors)
      .catch(() => setError("Failed to load vendors"));
  }, []);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setLoading(true);
    setError("");
    try {
      await createVendor({ name: name.trim(), website: website.trim() || undefined });
      setName("");
      setWebsite("");
      await fetchVendors();
    } catch (e) {
      setError(apiError(e, "Failed to create vendor"));
    } finally {
      setLoading(false);
    }
  };

  const handleRename = async (id: string, current: string) => {
    const next = prompt("Rename vendor", current);
    if (next === null || !next.trim() || next.trim() === current) return;
    try {
      await updateVendor(id, { name: next.trim() });
      await fetchVendors();
    } catch (e) {
      setError(apiError(e, "Failed to rename vendor"));
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this vendor and all associated data?")) return;
    try {
      await deleteVendor(id);
      await fetchVendors();
    } catch (e) {
      setError(apiError(e, "Failed to delete vendor"));
    }
  };

  return (
    <div className="vendor-list">
      <div className="dashboard-header">
        <h2>Vendors</h2>
        {canManage && (
          <button className="btn-primary-sm" onClick={() => setShowImport(true)}>
            Import CSV
          </button>
        )}
      </div>

      {error && <div className="error">{error}</div>}

      {canManage && (
      <form onSubmit={handleCreate} className="add-form">
        <input
          type="text"
          placeholder="Vendor name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
        <input
          type="url"
          placeholder="Website (optional)"
          value={website}
          onChange={(e) => setWebsite(e.target.value)}
        />
        <button type="submit" className="btn-primary" disabled={loading}>
          {loading ? "Adding..." : "Add Vendor"}
        </button>
      </form>
      )}

      {isAdmin && checked.size > 0 && (
        <div className="heading-actions" style={{ position: "relative", marginBottom: "0.5rem" }}>
          <span className="sub">{checked.size} selected</span>
          <button className="btn-secondary-sm" onClick={() => setTierMenu((v) => !v)}>
            Set tier ▾
          </button>
          <button className="btn-secondary-sm" onClick={() => setChecked(new Set())}>
            Clear
          </button>
          {tierMenu && (
            <ul
              className="item-list"
              style={{
                position: "absolute",
                top: "100%",
                zIndex: 10,
                minWidth: "12rem",
                padding: "8px 8px 0",
                background: "var(--ink-0)",
                border: "1px solid var(--line)",
                borderRadius: "var(--radius-sm)",
                boxShadow: "0 8px 24px -8px rgba(0, 0, 0, 0.6)",
              }}
            >
              {tiers.map((t) => (
                <li key={t.id} onClick={() => applyTier(t.id)}>{t.name}</li>
              ))}
              <li onClick={() => applyTier(null)}>No tier</li>
            </ul>
          )}
        </div>
      )}

      <ul className="item-list">
        {vendors.map((v) => (
          <li
            key={v.id}
            className={v.id === selectedId ? "selected" : ""}
            onClick={() => onSelect(v)}
          >
            {isAdmin && (
              <input
                type="checkbox"
                aria-label={`Select ${v.name}`}
                checked={checked.has(v.id)}
                onClick={(e) => e.stopPropagation()}
                onChange={() => toggleChecked(v.id)}
              />
            )}
            <div className="item-info">
              <strong>{v.name}</strong>
              {isAdmin && tierName(v.tier_id) && (
                <span className="status-badge" style={{ backgroundColor: "#5a7fa3", marginLeft: "0.5rem" }}>
                  {tierName(v.tier_id)}
                </span>
              )}
              {v.website && <span className="sub">{v.website}</span>}
            </div>
            {canManage && (
              <div className="item-actions">
                <button
                  className="btn-secondary-sm"
                  title="Rename"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleRename(v.id, v.name);
                  }}
                >
                  ✎
                </button>
                <button
                  className="btn-danger-sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDelete(v.id);
                  }}
                >
                  ×
                </button>
              </div>
            )}
          </li>
        ))}
        {vendors.length === 0 && (
          <li className="empty">
            {canManage ? "No vendors yet. Add one above." : "No vendors have been shared with you yet."}
          </li>
        )}
      </ul>
      {showImport && (
        <BulkImport
          onClose={() => setShowImport(false)}
          onImported={() => {
            setShowImport(false);
            fetchVendors();
          }}
        />
      )}
    </div>
  );
}
