import { useEffect, useState } from "react";
import { webhookApi } from "../api/client";
import { apiError } from "../api/errors";
import type {
  Webhook,
  WebhookCreate,
  WebhookUpdate,
  WebhookDelivery,
  WebhookEventType,
  WebhookTestResult,
} from "../types";

const ALL_EVENTS: WebhookEventType[] = [
  "new_page",
  "updated_page",
  "removed_page",
  "extraction_complete",
];

const EVENT_LABELS: Record<WebhookEventType, string> = {
  new_page: "New Page",
  updated_page: "Updated Page",
  removed_page: "Removed Page",
  extraction_complete: "Extraction Complete",
};

const EMPTY_FORM: WebhookCreate = {
  url: "",
  label: "",
  events: ["extraction_complete"],
  secret: "",
  is_active: true,
};

export function Webhooks() {
  const [webhooks, setWebhooks] = useState<Webhook[]>([]);
  const [form, setForm] = useState<WebhookCreate>(EMPTY_FORM);
  const [adding, setAdding] = useState(false);
  const [formError, setFormError] = useState("");
  const [actionStatus, setActionStatus] = useState<
    Record<string, { type: "ok" | "err"; msg: string }>
  >({});
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState<WebhookUpdate>({});
  const [deliveries, setDeliveries] = useState<Record<string, WebhookDelivery[]>>({});
  const [showDeliveries, setShowDeliveries] = useState<Record<string, boolean>>({});

  const refresh = () =>
    webhookApi.list().then((data) => setWebhooks(data.webhooks)).catch(() => {});
  useEffect(() => {
    refresh();
  }, []);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError("");
    setAdding(true);
    try {
      await webhookApi.create({
        ...form,
        label: form.label || undefined,
        secret: form.secret || undefined,
      });
      setForm(EMPTY_FORM);
      refresh();
    } catch (err) {
      setFormError(apiError(err, "Failed to create webhook"));
    } finally {
      setAdding(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this webhook?")) return;
    try {
      await webhookApi.remove(id);
      refresh();
    } catch (err) {
      setActionStatus((p) => ({
        ...p,
        [id]: { type: "err", msg: apiError(err, "Delete failed") },
      }));
    }
  };

  const handleTest = async (id: string) => {
    setActionStatus((p) => ({ ...p, [id]: { type: "ok", msg: "Sending..." } }));
    try {
      const result: WebhookTestResult = await webhookApi.test(id);
      setActionStatus((p) => ({
        ...p,
        [id]: {
          type: result.success ? "ok" : "err",
          msg: result.success
            ? `OK (${result.status_code})`
            : `Failed: ${result.error ?? "no response"}`,
        },
      }));
      refresh();
    } catch (err) {
      setActionStatus((p) => ({
        ...p,
        [id]: { type: "err", msg: apiError(err, "Test failed") },
      }));
    }
  };

  const handleToggleActive = async (w: Webhook) => {
    try {
      await webhookApi.update(w.id, { is_active: !w.is_active });
      refresh();
    } catch (err) {
      setActionStatus((p) => ({
        ...p,
        [w.id]: { type: "err", msg: apiError(err, "Update failed") },
      }));
    }
  };

  const handleStartEdit = (w: Webhook) => {
    setEditingId(w.id);
    setEditForm({
      url: w.url,
      label: w.label,
      events: w.events,
      // Secret is never returned by the API; leave blank — an empty value on
      // save leaves the stored secret unchanged.
      secret: "",
    });
  };

  const handleSaveEdit = async (id: string) => {
    try {
      await webhookApi.update(id, {
        ...editForm,
        label: editForm.label || undefined,
        secret: editForm.secret || undefined,
      });
      setEditingId(null);
      refresh();
    } catch (err) {
      setActionStatus((p) => ({
        ...p,
        [id]: { type: "err", msg: apiError(err, "Update failed") },
      }));
    }
  };

  const handleLoadDeliveries = async (id: string) => {
    const show = !showDeliveries[id];
    setShowDeliveries((p) => ({ ...p, [id]: show }));
    if (show) {
      try {
        const data = await webhookApi.deliveries(id);
        setDeliveries((p) => ({ ...p, [id]: data.deliveries }));
      } catch {
        // ignore
      }
    }
  };

  const toggleEvent = (
    events: WebhookEventType[],
    ev: WebhookEventType,
  ): WebhookEventType[] => {
    return events.includes(ev)
      ? events.filter((e) => e !== ev)
      : [...events, ev];
  };

  return (
    <div className="webhooks-view console-view">
      <h2>Webhooks</h2>
      <p className="sub view-intro">
        Configure outbound webhooks that fire when content changes are detected.
        Payloads are signed with HMAC-SHA256 via the{" "}
        <code>X-DocExtractor-Signature</code> header.
      </p>

      <form onSubmit={handleCreate} className="add-form">
        <input
          placeholder="Webhook URL (https://...)"
          value={form.url}
          onChange={(e) => setForm({ ...form, url: e.target.value })}
          required
          className="input-wide"
        />
        <input
          placeholder="Label (optional)"
          value={form.label ?? ""}
          onChange={(e) => setForm({ ...form, label: e.target.value })}
        />
        <input
          placeholder="HMAC secret (optional)"
          value={form.secret ?? ""}
          onChange={(e) => setForm({ ...form, secret: e.target.value })}
        />
        <div className="check-group">
          {ALL_EVENTS.map((ev) => (
            <label key={ev} className="check-row">
              <input
                type="checkbox"
                checked={form.events?.includes(ev) ?? false}
                onChange={() =>
                  setForm({
                    ...form,
                    events: toggleEvent(form.events ?? [], ev),
                  })
                }
              />
              {EVENT_LABELS[ev]}
            </label>
          ))}
        </div>
        <button type="submit" disabled={adding}>
          {adding ? "Adding..." : "Add webhook"}
        </button>
        {formError && <div className="error">{formError}</div>}
      </form>

      <ul className="item-list">
        {webhooks.map((w) => (
          <li key={w.id} className="non-clickable">
            <div className="item-info">
              {editingId === w.id ? (
                <div className="webhook-edit">
                  <input
                    value={editForm.url ?? ""}
                    onChange={(e) =>
                      setEditForm({ ...editForm, url: e.target.value })
                    }
                    className="input-wide"
                  />
                  <input
                    placeholder="Label"
                    value={editForm.label ?? ""}
                    onChange={(e) =>
                      setEditForm({ ...editForm, label: e.target.value })
                    }
                  />
                  <input
                    placeholder="HMAC secret (blank = keep current)"
                    value={editForm.secret ?? ""}
                    onChange={(e) =>
                      setEditForm({ ...editForm, secret: e.target.value })
                    }
                  />
                  <div className="check-group">
                    {ALL_EVENTS.map((ev) => (
                      <label key={ev} className="check-row">
                        <input
                          type="checkbox"
                          checked={editForm.events?.includes(ev) ?? false}
                          onChange={() =>
                            setEditForm({
                              ...editForm,
                              events: toggleEvent(editForm.events ?? [], ev),
                            })
                          }
                        />
                        {EVENT_LABELS[ev]}
                      </label>
                    ))}
                  </div>
                  <div className="edit-actions">
                    <button
                      type="button"
                      className="btn-primary-sm"
                      onClick={() => handleSaveEdit(w.id)}
                    >
                      Save
                    </button>
                    <button
                      type="button"
                      className="btn-secondary-sm"
                      onClick={() => setEditingId(null)}
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="card-head">
                    <span className={`status-badge ${w.is_active ? "is-on" : "is-off"}`}>
                      {w.is_active ? "active" : "inactive"}
                    </span>
                    {w.label && <strong>{w.label}</strong>}
                    <code className="webhook-url">{w.url}</code>
                  </div>
                  <div className="item-meta">
                    {w.events.map((ev) => (
                      <span key={ev} className="sub event-tag">
                        · {EVENT_LABELS[ev] || ev}
                      </span>
                    ))}
                    {w.has_secret && <span className="sub">· signed</span>}
                    {w.source_id && (
                      <span className="sub">· scoped to source</span>
                    )}
                  </div>
                  <div className="item-meta">
                    <span className="sub">
                      deliveries: {w.total_deliveries} ({w.total_failures} failed)
                    </span>
                    {w.last_status_code !== null && (
                      <span className="sub">
                        · last: {w.last_status_code}
                      </span>
                    )}
                    {w.last_attempt_at && (
                      <span className="sub">
                        · {new Date(w.last_attempt_at).toLocaleString()}
                      </span>
                    )}
                  </div>
                  {w.last_error && (
                    <div className="error webhook-error">{w.last_error}</div>
                  )}
                  {actionStatus[w.id] && (
                    <div
                      className={`webhook-status ${actionStatus[w.id].type === "ok" ? "sub" : "error"}`}
                    >
                      {actionStatus[w.id].msg}
                    </div>
                  )}

                  {showDeliveries[w.id] && deliveries[w.id] && (
                    <div className="deliveries-block">
                      <h4 className="deliveries-title">Recent Deliveries</h4>
                      <table className="subtable">
                        <thead>
                          <tr>
                            <th>Time</th>
                            <th>Event</th>
                            <th>Status</th>
                            <th>Attempt</th>
                            <th>Error</th>
                          </tr>
                        </thead>
                        <tbody>
                          {deliveries[w.id].map((d) => (
                            <tr key={d.id}>
                              <td>{new Date(d.created_at).toLocaleString()}</td>
                              <td>{d.event_type}</td>
                              <td>
                                <span className={d.success ? "ok" : "bad"}>
                                  {d.success
                                    ? (d.status_code ?? "OK")
                                    : (d.status_code ?? "failed")}
                                </span>
                              </td>
                              <td>{d.attempt}</td>
                              <td className="bad">{d.error ?? ""}</td>
                            </tr>
                          ))}
                          {deliveries[w.id].length === 0 && (
                            <tr>
                              <td colSpan={5} className="sub">No deliveries yet.</td>
                            </tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              )}
            </div>

            {editingId !== w.id && (
              <div className="item-actions">
                <button
                  type="button"
                  className="btn-secondary-sm"
                  onClick={() => handleTest(w.id)}
                >
                  Test
                </button>
                <button
                  type="button"
                  className="btn-secondary-sm"
                  onClick={() => handleLoadDeliveries(w.id)}
                >
                  {showDeliveries[w.id] ? "Hide deliveries" : "Deliveries"}
                </button>
                <button
                  type="button"
                  className="btn-secondary-sm"
                  onClick={() => handleStartEdit(w)}
                >
                  Edit
                </button>
                <button
                  type="button"
                  className="btn-secondary-sm"
                  onClick={() => handleToggleActive(w)}
                >
                  {w.is_active ? "Disable" : "Enable"}
                </button>
                <button
                  type="button"
                  className="btn-danger-sm"
                  onClick={() => handleDelete(w.id)}
                >
                  ×
                </button>
              </div>
            )}
          </li>
        ))}
        {webhooks.length === 0 && (
          <li className="empty">
            No webhooks configured yet. Add one above to receive content change
            notifications.
          </li>
        )}
      </ul>
    </div>
  );
}