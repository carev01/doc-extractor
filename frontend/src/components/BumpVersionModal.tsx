import { useEffect, useState } from "react";
import type { Product, DocumentationSource, BumpPlanEntry } from "../types";
import { bumpProductVersion, previewProductVersionBump } from "../api/client";
import UrlTemplate from "./UrlTemplate";

interface Props {
  product: Product & { version: string | null };
  sources: DocumentationSource[];
  onClose: () => void;
  onBumped: (newVersion: string) => void;
}

const STATUS_LABEL: Record<BumpPlanEntry["status"], string> = {
  ok: "ready",
  resolved: "resolved",
  unresolved: "not found",
};

const STATUS_CLASS: Record<BumpPlanEntry["status"], string> = {
  ok: "is-ok",
  resolved: "is-ok",
  unresolved: "is-warn",
};

/** The dry-run we hold, tagged with the version it describes. `entries: null`
 *  means the preview call itself failed, which is distinct from "not asked yet"
 *  — the former lets the operator through to the server's own 409 check, the
 *  latter must not. Keying by version is what lets "is this preview current?" be
 *  derived during render instead of invalidated by a setState inside the effect,
 *  which triggers cascading renders (react-hooks/set-state-in-effect). */
interface Preview {
  version: string;
  entries: BumpPlanEntry[] | null;
}

export default function BumpVersionModal({ product, sources, onClose, onBumped }: Props) {
  const [newVersion, setNewVersion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);

  const templated = sources.filter((s) => s.url_template && s.url_template.includes("{version}"));
  const v = newVersion.trim();

  const wantsPreview = v !== "" && v !== product.version && templated.length > 0;
  // Narrow `preview` inline rather than through a boolean, so this still
  // type-checks if strictNullChecks is ever turned on.
  const current = preview !== null && preview.version === v ? preview : null;
  const settled = current !== null;
  const entries = current?.entries ?? null;
  const checking = wantsPreview && !settled;
  const previewFailed = settled && entries === null;

  // Dry-run as the operator types. The new URL can't be computed in the browser
  // once a template carries {rev}: that token is the vendor's, and only the
  // backend's profile knows where to read it. Debounced because each check is a
  // real fetch against the vendor's landing page. Every state write happens in
  // the timeout callback, never synchronously in the effect body.
  useEffect(() => {
    if (!wantsPreview || settled) return;
    let cancelled = false;
    const t = setTimeout(async () => {
      try {
        const res = await previewProductVersionBump(product.id, v);
        if (!cancelled) setPreview({ version: v, entries: res.sources });
      } catch {
        if (!cancelled) setPreview({ version: v, entries: null });
      }
    }, 450);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [v, wantsPreview, settled, product.id]);

  const blocked = (entries ?? []).filter((e) => e.status === "unresolved");
  const canSubmit = wantsPreview && !checking && !busy;
  const needsForce = blocked.length > 0;

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError("");
    try {
      const res = await bumpProductVersion(product.id, v, needsForce);
      onBumped(res.version);
    } catch (e: unknown) {
      // The backend refuses an unresolved bump with a 409 before it touches
      // previous_version — surface its reason rather than a blanket failure.
      const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      const message =
        detail && typeof detail === "object" && "message" in detail
          ? String((detail as { message: unknown }).message)
          : typeof detail === "string"
            ? detail
            : "Bump failed";
      setError(message);
      setBusy(false);
    }
  };

  return (
    <div className="picker-backdrop" onClick={onClose}>
      <div className="picker-panel is-wide" onClick={(e) => e.stopPropagation()}>
        <h3>
          Bump {product.name} from {product.version} → {v || "?"}
        </h3>
        {/* type is explicit: .picker-panel styles its input through an
            [type="text"] attribute selector, which an untyped input never
            matches — it would render as a bare UA control inside the panel. */}
        <input
          autoFocus
          type="text"
          placeholder="New version (e.g. 11.2.0.1)"
          value={newVersion}
          onChange={(e) => setNewVersion(e.target.value)}
        />
        <p className="muted">
          {templated.length} templated source(s) will be rewritten and re-extracted
          {checking ? " — checking with the vendor…" : ":"}
        </p>
        <ul className="bump-plan">
          {templated.map((s) => {
            const entry = entries?.find((p) => p.source_id === s.id);
            return (
              <li key={s.id} className="bump-plan-row">
                <div className="bump-plan-head">
                  <span className="bump-plan-name">{s.name}</span>
                  {entry && (
                    <span className={`status-badge ${STATUS_CLASS[entry.status]}`}>
                      {STATUS_LABEL[entry.status]}
                    </span>
                  )}
                </div>
                <UrlTemplate value={s.url_template as string} />
                {entry ? (
                  <>
                    <div className="bump-plan-url">
                      <span className="muted">→ </span>
                      <code>{entry.resolved_url}</code>
                    </div>
                    {entry.status === "resolved" && (
                      <div className="bump-plan-note">
                        Vendor build <code>{entry.revision}</code> for {v}
                      </div>
                    )}
                    {entry.status === "unresolved" && (
                      <div className="bump-plan-note is-warn-text">
                        {entry.detail} — keeping the current release's token, which
                        this URL will 404 on until a run resolves it.
                      </div>
                    )}
                  </>
                ) : (
                  <div className="bump-plan-url muted">
                    {checking
                      ? "resolving…"
                      : previewFailed
                        ? "preview unavailable — the bump will be checked server-side"
                        : "enter a version to preview"}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
        {sources.length > templated.length && (
          <p className="muted">
            {sources.length - templated.length} non-templated source(s) unaffected.
          </p>
        )}
        {error && <span className="error-inline">{error}</span>}
        <div className="picker-actions">
          <button
            type="button"
            className="btn-primary"
            disabled={!canSubmit}
            onClick={submit}
          >
            {busy ? "Bumping…" : needsForce ? "Bump anyway & re-extract" : "Bump & re-extract"}
          </button>
          <button type="button" className="btn-secondary-sm" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
