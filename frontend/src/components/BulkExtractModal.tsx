import { useEffect, useMemo, useState } from "react";
import type { BulkExtractPreview, BulkExtractResult, BulkExtractSkipReason } from "../types";
import { bulkExtract, previewBulkExtract } from "../api/client";
import { apiError } from "../api/errors";
import SourceTreePicker, { type TreeProduct } from "./SourceTreePicker";
import type { BatchScope } from "./BatchExportModal";

interface Props {
  scope: BatchScope;
  onClose: () => void;
  /** Called after runs were queued, so the caller can refresh statuses. */
  onQueued?: () => void;
}

const REASON: Record<BulkExtractSkipReason, string> = {
  active_run: "already queued or running",
  session_expired: "login expired",
  no_write_access: "no write access",
  not_found: "not found",
};

/**
 * Queue an ordinary extraction for the chosen sources of a product or vendor.
 * Runs go through the normal queue, one at a time, so there is nothing to
 * manage afterwards: progress shows on each source as usual. The one-source
 * escape hatches (force re-scrape, "Extract anyway") are deliberately absent.
 */
export default function BulkExtractModal({ scope, onClose, onQueued }: Props) {
  const [preview, setPreview] = useState<BulkExtractPreview | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<BulkExtractResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    previewBulkExtract(scope.kind === "product" ? { product_id: scope.id } : { vendor_id: scope.id })
      .then((p) => {
        if (cancelled) return;
        setPreview(p);
        // Everything that can run starts ticked; the rest show why not.
        setSelected(
          new Set(p.products.flatMap((pr) => pr.sources.filter((s) => s.eligible).map((s) => s.source_id))),
        );
      })
      .catch((e) => {
        if (!cancelled) setError(apiError(e, `Could not read this ${scope.kind}'s sources`));
      });
    return () => {
      cancelled = true;
    };
  }, [scope.kind, scope.id]);

  const tree: TreeProduct[] = useMemo(
    () =>
      (preview?.products ?? []).map((p) => ({
        id: p.product_id,
        name: p.product_name,
        sources: p.sources.map((s) => ({
          id: s.source_id,
          name: s.source_name,
          disabled: !s.eligible,
          note: s.reason ? REASON[s.reason] : undefined,
        })),
      })),
    [preview],
  );
  const names = useMemo(
    () => new Map(tree.flatMap((p) => p.sources.map((s) => [s.id, s.name] as const))),
    [tree],
  );
  const total = tree.reduce((n, p) => n + p.sources.length, 0);
  const unavailable = tree.reduce((n, p) => n + p.sources.filter((s) => s.disabled).length, 0);

  const start = async () => {
    setBusy(true);
    setError("");
    try {
      setResult(await bulkExtract([...selected]));
      onQueued?.();
    } catch (e) {
      setError(apiError(e, "Could not queue the extractions"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="picker-backdrop" onClick={onClose}>
      <div className="picker-panel is-wide" onClick={(e) => e.stopPropagation()}>
        <h3>
          {scope.kind === "product" ? "Extract sources" : "Extract products"} — {scope.name}
        </h3>

        {!result && (
          <>
            {preview ? (
              total === 0 ? (
                <p className="muted">No sources here yet.</p>
              ) : (
                <SourceTreePicker
                  products={tree}
                  selected={selected}
                  onChange={setSelected}
                  flat={scope.kind === "product"}
                />
              )
            ) : (
              !error && <p className="muted">Reading this {scope.kind}'s sources…</p>
            )}
            {preview && total > 0 && (
              <p className="muted">
                {selected.size} to extract
                {unavailable > 0 && ` · ${unavailable} unavailable`}
                {" · runs one at a time, in the usual queue"}
              </p>
            )}
          </>
        )}

        {result && (
          <>
            <p>
              Queued {result.queued.length} extraction(s)
              {result.skipped.length > 0 && `, skipped ${result.skipped.length}`}.
            </p>
            {result.skipped.length > 0 && (
              <ul className="bump-plan">
                {result.skipped.map((s) => (
                  <li key={s.source_id} className="bump-plan-row">
                    <span className="bump-plan-name">{names.get(s.source_id) ?? s.source_id}</span>
                    <span className="hint"> — {REASON[s.reason]}</span>
                  </li>
                ))}
              </ul>
            )}
            <p className="muted">Progress shows on each source as it runs.</p>
          </>
        )}

        {error && <span className="error-inline">{error}</span>}

        <div className="picker-actions">
          {!result && (
            <button
              type="button"
              className="btn-primary"
              disabled={busy || selected.size === 0}
              onClick={start}
            >
              {busy ? "Queueing…" : `Extract ${selected.size} source(s)`}
            </button>
          )}
          <button type="button" className="btn-secondary-sm" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
