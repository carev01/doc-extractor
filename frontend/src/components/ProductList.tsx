import { useState, useEffect, useCallback } from "react";
import type { Vendor, Product } from "../types";
import { listProducts, createProduct, updateProduct, deleteProduct } from "../api/client";
import { apiError } from "../api/errors";
import type { Access } from "../access";
import BatchExportModal from "./BatchExportModal";
import BulkExtractModal from "./BulkExtractModal";

interface Props {
  vendor: Vendor;
  onSelect: (product: Product) => void;
  selectedId?: string;
  access: Access;
}

export default function ProductList({ vendor, onSelect, selectedId, access }: Props) {
  // Product writes need a read_write grant on the owning vendor
  // (require_vendor_write). Read-only access leaves the rows navigable and
  // nothing else.
  const canWrite = access.canWriteVendor(vendor.id);
  const [products, setProducts] = useState<Product[]>([]);
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [showVendorExport, setShowVendorExport] = useState(false);
  const [showVendorExtract, setShowVendorExtract] = useState(false);

  const fetchProducts = useCallback(async () => {
    try {
      const data = await listProducts(vendor.id);
      setProducts(data.products);
    } catch {
      setError("Failed to load products");
    }
  }, [vendor.id]);

  useEffect(() => {
    listProducts(vendor.id)
      .then((data) => setProducts(data.products))
      .catch(() => setError("Failed to load products"));
  }, [vendor.id]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setLoading(true);
    setError("");
    try {
      await createProduct({ vendor_id: vendor.id, name: name.trim() });
      setName("");
      await fetchProducts();
    } catch (e) {
      setError(apiError(e, "Failed to create product"));
    } finally {
      setLoading(false);
    }
  };

  const handleRename = async (id: string, current: string) => {
    const next = prompt("Rename product", current);
    if (next === null || !next.trim() || next.trim() === current) return;
    try {
      await updateProduct(id, { name: next.trim() });
      await fetchProducts();
    } catch (e) {
      setError(apiError(e, "Failed to rename product"));
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this product and all its documentation sources?")) return;
    try {
      await deleteProduct(id);
      await fetchProducts();
    } catch (e) {
      setError(apiError(e, "Failed to delete product"));
    }
  };

  return (
    <div className="product-list">
      <div className="product-heading">
        <h2>Products — {vendor.name}</h2>
        <div className="heading-actions">
          {/* Exporting only reads, so it is offered to read-only grants too —
              the same as the per-product "Export all sources". */}
          {products.length > 1 && (
            <button
              type="button"
              className="btn-secondary-sm"
              title="Export the sources of several products of this vendor in one go"
              onClick={() => setShowVendorExport(true)}
            >
              Export products
            </button>
          )}
          {/* Extracting writes, so it needs a read_write grant — every source is
              still authorized again server-side. */}
          {canWrite && products.length > 0 && (
            <button
              type="button"
              className="btn-secondary-sm"
              title="Queue an extraction for the chosen sources of this vendor's products"
              onClick={() => setShowVendorExtract(true)}
            >
              Extract products
            </button>
          )}
        </div>
      </div>
      {showVendorExtract && (
        <BulkExtractModal
          scope={{ kind: "vendor", id: vendor.id, name: vendor.name }}
          onClose={() => setShowVendorExtract(false)}
        />
      )}
      {showVendorExport && (
        <BatchExportModal
          scope={{ kind: "vendor", id: vendor.id, name: vendor.name }}
          onClose={() => setShowVendorExport(false)}
        />
      )}

      {error && <div className="error">{error}</div>}

      {canWrite && (
        <form onSubmit={handleCreate} className="add-form">
          <input
            type="text"
            placeholder="Product name (e.g. 'NetBackup')"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
          <button type="submit" className="btn-primary" disabled={loading}>
            {loading ? "Adding..." : "Add Product"}
          </button>
        </form>
      )}

      <ul className="item-list">
        {products.map((p) => (
          <li
            key={p.id}
            className={p.id === selectedId ? "selected" : ""}
            onClick={() => onSelect(p)}
          >
            <div className="item-info">
              <strong>{p.name}</strong>
            </div>
            {canWrite && (
              <div className="item-actions">
                <button
                  className="btn-secondary-sm"
                  title="Rename"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleRename(p.id, p.name);
                  }}
                >
                  ✎
                </button>
                <button
                  className="btn-danger-sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDelete(p.id);
                  }}
                >
                  ×
                </button>
              </div>
            )}
          </li>
        ))}
        {products.length === 0 && (
          <li className="empty">
            {canWrite ? "No products yet. Add one above." : "No products for this vendor yet."}
          </li>
        )}
      </ul>
    </div>
  );
}
