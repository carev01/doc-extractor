import { useState, useEffect } from "react";
import type { Vendor } from "../types";
import { listVendors, createVendor, updateVendor, deleteVendor } from "../api/client";
import { apiError } from "../api/errors";
import BulkImport from "./BulkImport";

interface Props {
  onSelect: (vendor: Vendor) => void;
  selectedId?: string;
}

export default function VendorList({ onSelect, selectedId }: Props) {
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [name, setName] = useState("");
  const [website, setWebsite] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [showImport, setShowImport] = useState(false);

  const fetchVendors = async () => {
    try {
      const data = await listVendors();
      setVendors(data.vendors);
    } catch {
      setError("Failed to load vendors");
    }
  };

  useEffect(() => {
    listVendors()
      .then((data) => setVendors(data.vendors))
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
        <button className="btn-primary-sm" onClick={() => setShowImport(true)}>
          Import CSV
        </button>
      </div>

      {error && <div className="error">{error}</div>}

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
        <button type="submit" disabled={loading}>
          {loading ? "Adding..." : "Add Vendor"}
        </button>
      </form>

      <ul className="item-list">
        {vendors.map((v) => (
          <li
            key={v.id}
            className={v.id === selectedId ? "selected" : ""}
            onClick={() => onSelect(v)}
          >
            <div className="item-info">
              <strong>{v.name}</strong>
              {v.website && <span className="sub">{v.website}</span>}
            </div>
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
          </li>
        ))}
        {vendors.length === 0 && (
          <li className="empty">No vendors yet. Add one above.</li>
        )}
      </ul>
      {showImport && (
        <BulkImport
          onClose={() => setShowImport(false)}
          onImported={() => {
            setShowImport(false);
            listVendors()
              .then((data) => setVendors(data.vendors))
              .catch(() => setError("Failed to load vendors"));
          }}
        />
      )}
    </div>
  );
}
