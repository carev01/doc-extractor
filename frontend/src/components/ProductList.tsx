import { useState, useEffect, useCallback } from "react";
import type { Vendor, Product } from "../types";
import { listProducts, createProduct, deleteProduct } from "../api/client";

interface Props {
  vendor: Vendor;
  onSelect: (product: Product) => void;
  selectedId?: string;
}

export default function ProductList({ vendor, onSelect, selectedId }: Props) {
  const [products, setProducts] = useState<Product[]>([]);
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const fetchProducts = useCallback(async () => {
    try {
      const data = await listProducts(vendor.id);
      setProducts(data.products);
    } catch {
      setError("Failed to load products");
    }
  }, [vendor.id]);

  useEffect(() => {
    fetchProducts();
  }, [fetchProducts]);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setLoading(true);
    setError("");
    try {
      await createProduct({ vendor_id: vendor.id, name: name.trim() });
      setName("");
      await fetchProducts();
    } catch (e: any) {
      setError(e.response?.data?.detail || "Failed to create product");
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this product and all its documentation sources?")) return;
    try {
      await deleteProduct(id);
      await fetchProducts();
    } catch (e: any) {
      setError(e.response?.data?.detail || "Failed to delete product");
    }
  };

  return (
    <div className="product-list">
      <h2>Products — {vendor.name}</h2>

      {error && <div className="error">{error}</div>}

      <form onSubmit={handleCreate} className="add-form">
        <input
          type="text"
          placeholder="Product name (e.g. 'NetBackup')"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
        <button type="submit" disabled={loading}>
          {loading ? "Adding..." : "Add Product"}
        </button>
      </form>

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
            <button
              className="btn-danger-sm"
              onClick={(e) => {
                e.stopPropagation();
                handleDelete(p.id);
              }}
            >
              ×
            </button>
          </li>
        ))}
        {products.length === 0 && (
          <li className="empty">No products yet. Add one above.</li>
        )}
      </ul>
    </div>
  );
}
