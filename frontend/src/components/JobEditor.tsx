import { useEffect, useMemo, useState } from "react";
import type { CatalogVendor, Job, JobSchedule, ScheduleKind, Tier } from "../types";
import { createJob, previewJobSchedule, setJobCoverage, setJobExclusions, updateJob } from "../api/client";
import { apiError } from "../api/errors";
import SourceTreePicker, { type TreeVendor } from "./SourceTreePicker";
import {
  INTERVALS,
  MONTHS,
  WEEKDAYS,
  coverageLine,
  fmtInTz,
  indexCatalog,
  overlapWith,
  resolveJob,
  resolveTargets,
} from "./jobHelpers";

interface Props {
  /** The job being edited, or null to create one. */
  job: Job | null;
  jobs: Job[];
  tiers: Tier[];
  catalog: CatalogVendor[];
  /** True until THIS open's catalog fetch has resolved (the catalog may be a stale earlier read). */
  catalogLoading: boolean;
  catalogError: string;
  onRetryCatalog: () => void;
  onClose: () => void;
  /** Called after anything was written, so the list can refresh. */
  onSaved: () => void;
}

/** "Repeat" choices; the two monthly kinds share one, split by "on". */
type Repeat = "" | "daily" | "weekly" | "monthly";
/** Monthly "on": a day number, or the first/last weekday. */
type MonthlyOn = "day" | "first" | "last";

const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const TIMEZONES: string[] = (() => {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return [];
  }
})();

/** The month after the current one (1-12) as the calendar reads in `tz`, like the backend default. */
function nextMonthIn(tz: string): number {
  const now = new Date();
  let month = now.getMonth() + 1;
  try {
    const m = new Intl.DateTimeFormat("en-US", { timeZone: tz, month: "numeric" }).format(now);
    if (Number(m) >= 1) month = Number(m);
  } catch {
    /* unknown zone: fall back to the browser's month */
  }
  return (month % 12) + 1;
}

function repeatOf(kind: string | null): Repeat {
  if (kind === "daily" || kind === "weekly") return kind;
  if (kind === "monthly_day" || kind === "monthly_nth") return "monthly";
  return "";
}

/**
 * Create or edit a job: name, enabled, tier, explicit picks (vendor → product →
 * source tree), schedule with a live next-three-runs preview, and a live
 * coverage summary. Save writes the fields, then the picks (``PUT /coverage``,
 * which the backend folds into dynamic vendor/product targets).
 */
export default function JobEditor({
  job,
  jobs,
  tiers,
  catalog,
  catalogLoading,
  catalogError,
  onRetryCatalog,
  onClose,
  onSaved,
}: Props) {
  const idx = useMemo(() => indexCatalog(catalog), [catalog]);
  const initialPicks = useMemo(
    () => (job ? resolveTargets(job.targets, idx) : new Set<string>()),
    [job, idx],
  );

  const [savedId, setSavedId] = useState<string | null>(job?.id ?? null);
  const [name, setName] = useState(job?.name ?? "");
  const [enabled, setEnabled] = useState(job?.enabled ?? false);
  const [tierId, setTierId] = useState<string | null>(job?.tier_id ?? null);
  const [picks, setPicksState] = useState<Set<string>>(initialPicks);
  const [touched, setTouched] = useState(false);
  const [seenInitial, setSeenInitial] = useState(initialPicks);
  // The catalog is re-read when the editor opens; until the tree is touched,
  // follow it so a fresh catalog never reads as an edit of the picks.
  if (seenInitial !== initialPicks) {
    setSeenInitial(initialPicks);
    if (!touched) setPicksState(initialPicks);
  }
  const setPicks = (next: Set<string>) => {
    setTouched(true);
    setPicksState(next);
  };
  const [filter, setFilter] = useState("");

  const initialExcluded = useMemo(() => new Set(job?.excluded_vendor_ids ?? []), [job]);
  const [excluded, setExcluded] = useState<Set<string>>(initialExcluded);
  const [showAllTierVendors, setShowAllTierVendors] = useState(false);
  const tierVendors = useMemo(
    () => (tierId ? catalog.filter((v) => v.tier_id === tierId) : []),
    [catalog, tierId],
  );
  // Exclusions of vendors outside the chosen tier are inert; only these count.
  const excludedInTier = tierVendors.filter((v) => excluded.has(v.vendor_id)).length;
  const toggleExcluded = (id: string) => {
    const next = new Set(excluded);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setExcluded(next);
  };

  const [repeat, setRepeat] = useState<Repeat>(repeatOf(job?.schedule_kind ?? null));
  const [monthlyOn, setMonthlyOn] = useState<MonthlyOn>(
    job?.schedule_kind === "monthly_nth" ? (job.nth ?? "first") : "day",
  );
  const [interval, setIntervalMonths] = useState(job?.interval_months ?? 1);
  // Unless the user picks one, the default (next month, in the job's timezone)
  // is derived, so it follows the timezone and matches the backend's default.
  const [startPicked, setStartPicked] = useState<number | null>(job?.start_month ?? null);
  const [dayOfMonth, setDayOfMonth] = useState(job?.day_of_month ?? 1);
  const [weekday, setWeekday] = useState(job?.weekday ?? 0);
  const [time, setTime] = useState(job?.time_of_day ?? "02:00");
  const [tz, setTz] = useState(job?.timezone ?? browserTz);
  const startMonth = startPicked ?? nextMonthIn(tz);

  // The preview remembers which schedule it answers, so a stale one can be dimmed.
  const [preview, setPreview] = useState<{ key: string; runs: string[] }>({ key: "", runs: [] });
  const [previewError, setPreviewError] = useState<{ key: string; msg: string }>({ key: "", msg: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const kind: ScheduleKind | null =
    repeat === "" ? null : repeat === "monthly" ? (monthlyOn === "day" ? "monthly_day" : "monthly_nth") : repeat;

  const schedule: JobSchedule = useMemo(
    () => ({
      schedule_kind: kind,
      time_of_day: time,
      timezone: tz,
      weekday: kind === "weekly" || kind === "monthly_nth" ? weekday : null,
      interval_months: repeat === "monthly" ? interval : null,
      start_month: repeat === "monthly" ? startMonth : null,
      day_of_month: kind === "monthly_day" ? dayOfMonth : null,
      nth: kind === "monthly_nth" ? (monthlyOn as "first" | "last") : null,
    }),
    [kind, repeat, time, tz, weekday, interval, startMonth, dayOfMonth, monthlyOn],
  );

  const scheduleKey = JSON.stringify(schedule);

  // Live "Next runs": debounced so typing a timezone doesn't fire per keystroke.
  useEffect(() => {
    if (!schedule.schedule_kind) return;
    let cancelled = false;
    const { schedule_kind, ...rest } = schedule;
    const t = window.setTimeout(() => {
      previewJobSchedule({ ...rest, kind: schedule_kind })
        .then((runs) => {
          if (cancelled) return;
          setPreview({ key: JSON.stringify(schedule), runs });
          setPreviewError({ key: "", msg: "" });
        })
        .catch((e) => {
          if (cancelled) return;
          setPreview({ key: "", runs: [] });
          setPreviewError({ key: JSON.stringify(schedule), msg: apiError(e, "Could not preview this schedule") });
        });
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [schedule]);

  const tierName = (id: string | null) => tiers.find((t) => t.id === id)?.name;

  const tree: TreeVendor[] = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const tn = tiers.find((t) => t.id === tierId)?.name;
    return catalog
      .filter(
        (v) =>
          !q ||
          v.vendor_name.toLowerCase().includes(q) ||
          v.products.some((p) => p.product_name.toLowerCase().includes(q)),
      )
      .map((v) => ({
        id: v.vendor_id,
        name: v.vendor_name,
        note: tierId && v.tier_id === tierId ? `in ${tn} — already covered` : undefined,
        products: v.products.map((p) => ({
          id: p.product_id,
          name: p.product_name,
          sources: p.sources.map((s) => ({
            id: s.source_id,
            name: s.source_name,
            note: s.auth_realm ? `login: ${s.auth_realm}` : undefined,
          })),
        })),
      }));
  }, [catalog, filter, tierId, tiers]);

  const summary = useMemo(() => {
    const covered = resolveJob(tierId, picks, idx, excluded);
    const ov = overlapWith(covered, savedId, jobs, idx);
    return coverageLine(
      {
        sources: covered.size,
        products: new Set([...covered].map((s) => idx.productOf.get(s))).size,
        overlap_sources: ov.count,
        login_sources: [...covered].filter((s) => idx.login.has(s)).length,
        excluded_vendors: excludedInTier,
      },
      ov.names,
    );
  }, [tierId, picks, idx, savedId, jobs, excluded, excludedInTier]);

  // Vendor/product targets the (fresh) catalog no longer has any sources for.
  // They survive a save that leaves the tree alone; editing the tree drops them.
  const emptyPicks = useMemo(() => {
    if (!job || catalogLoading || catalogError) return [];
    const names = new Map<string, string>();
    for (const v of catalog) {
      names.set(v.vendor_id, v.vendor_name);
      for (const p of v.products) names.set(p.product_id, `${v.vendor_name} › ${p.product_name}`);
    }
    return [...job.targets.vendor_ids, ...job.targets.product_ids]
      .filter((id) => (idx.sourcesOf.get(id) ?? []).length === 0)
      .map((id) => names.get(id) ?? `(removed ${id.slice(0, 8)})`);
  }, [job, catalog, idx, catalogLoading, catalogError]);

  const picksDirty =
    picks.size !== initialPicks.size || [...picks].some((s) => !initialPicks.has(s));

  const exclusionsDirty =
    excluded.size !== initialExcluded.size || [...excluded].some((v) => !initialExcluded.has(v));

  const save = async () => {
    if (!name.trim()) {
      setError("A job needs a name");
      return;
    }
    if (
      picksDirty &&
      emptyPicks.length > 0 &&
      !confirm(
        `These empty picks will be dropped from the job because the source tree was edited:\n\n${emptyPicks.join("\n")}\n\nContinue?`,
      )
    )
      return;
    // A tier change leaves exclusions of vendors outside the new tier inert;
    // offer to clear them rather than keep them silently.
    let exclusionsToSave = excluded;
    let exclusionsChanged = exclusionsDirty;
    if (job && tierId !== job.tier_id && excluded.size > 0) {
      const inNewTier = new Set(tierVendors.map((v) => v.vendor_id));
      const stale = [...excluded].filter((v) => !inNewTier.has(v));
      if (stale.length > 0 && confirm(`Clear ${stale.length} exclusion${stale.length === 1 ? "" : "s"} that no longer apply?`)) {
        exclusionsToSave = new Set([...excluded].filter((v) => inNewTier.has(v)));
        exclusionsChanged = true;
      }
    }
    setBusy(true);
    setError("");
    let wrote = false;
    const body = { name: name.trim(), enabled: enabled && kind !== null, tier_id: tierId, ...schedule };
    try {
      let id = savedId;
      if (id) {
        await updateJob(id, body);
        wrote = true;
      } else {
        id = (await createJob(body)).id;
        wrote = true;
        setSavedId(id);
      }
      // Only rewrite the picks when they changed: an untouched tree must not
      // replace a dynamic target whose sources are all gone (nothing to tick).
      if (picksDirty || (!job && picks.size > 0)) await setJobCoverage(id, [...picks]);
      if (exclusionsChanged) await setJobExclusions(id, [...exclusionsToSave]);
      onSaved();
      onClose();
    } catch (e) {
      setError(apiError(e, "Failed to save the job"));
      // Only refresh the list if something was actually written before the failure.
      if (wrote) onSaved();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="picker-backdrop" onClick={onClose}>
      <div className="picker-panel is-wide job-editor" onClick={(e) => e.stopPropagation()}>
        <h3>{savedId ? `Edit job — ${job?.name ?? name}` : "New job"}</h3>

        <div className="job-editor-body">
          <div className="export-row">
            <label>
              Name
              <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
            </label>
            <label>
              Tier
              <select value={tierId ?? ""} onChange={(e) => setTierId(e.target.value || null)}>
                <option value="">(no tier)</option>
                {tiers.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name} — {t.vendor_count} vendor(s)
                  </option>
                ))}
              </select>
            </label>
            <label className="job-enabled">
              <span>
                <input
                  type="checkbox"
                  checked={enabled && kind !== null}
                  disabled={kind === null}
                  onChange={(e) => setEnabled(e.target.checked)}
                />{" "}
                Enabled
              </span>
              {kind === null && <span className="hint">needs a schedule</span>}
            </label>
          </div>

          <h4>Schedule</h4>
          <div className="schedule-fields">
            <select value={repeat} onChange={(e) => setRepeat(e.target.value as Repeat)}>
              <option value="">No schedule (manual only)</option>
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
              <option value="monthly">Every N months</option>
            </select>
            {repeat === "monthly" && (
              <>
                <select value={interval} onChange={(e) => setIntervalMonths(Number(e.target.value))}>
                  {INTERVALS.map((n) => (
                    <option key={n} value={n}>
                      {n === 1 ? "every month" : `every ${n} months`}
                    </option>
                  ))}
                </select>
                {interval > 1 && (
                  <label className="sub">
                    starting{" "}
                    <select value={startMonth} onChange={(e) => setStartPicked(Number(e.target.value))}>
                      {MONTHS.map((m, i) => (
                        <option key={m} value={i + 1}>
                          {m}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <label className="sub">
                  on{" "}
                  <select value={monthlyOn} onChange={(e) => setMonthlyOn(e.target.value as MonthlyOn)}>
                    <option value="day">day</option>
                    <option value="first">the first</option>
                    <option value="last">the last</option>
                  </select>
                </label>
                {monthlyOn === "day" && (
                  <select value={dayOfMonth} onChange={(e) => setDayOfMonth(Number(e.target.value))}>
                    {Array.from({ length: 28 }, (_, i) => i + 1).map((d) => (
                      <option key={d} value={d}>
                        {d}
                      </option>
                    ))}
                  </select>
                )}
              </>
            )}
            {(repeat === "weekly" || (repeat === "monthly" && monthlyOn !== "day")) && (
              <select value={weekday} onChange={(e) => setWeekday(Number(e.target.value))}>
                {WEEKDAYS.map((d, i) => (
                  <option key={d} value={i}>
                    {d}
                  </option>
                ))}
              </select>
            )}
            {repeat !== "" && (
              <>
                <label className="sub">
                  at <input type="time" value={time} onChange={(e) => setTime(e.target.value)} />
                </label>
                <input
                  className="schedule-tz"
                  list="job-timezones"
                  value={tz}
                  onChange={(e) => setTz(e.target.value)}
                  aria-label="Timezone"
                />
                <datalist id="job-timezones">
                  {TIMEZONES.map((z) => (
                    <option key={z} value={z} />
                  ))}
                </datalist>
              </>
            )}
          </div>
          {repeat !== "" && (
            <div className="job-next-runs">
              <span className="sub">
                Next runs{!enabled && " (if enabled)"}:
              </span>
              {previewError.key === scheduleKey ? (
                <span className="error-inline">{previewError.msg}</span>
              ) : preview.key !== scheduleKey ? (
                <span className="sub">updating…</span>
              ) : (
                <ul>
                  {preview.runs.map((r) => (
                    <li key={r} className="sub">
                      {fmtInTz(r, tz)}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {tierId && tierVendors.length > 0 && (
            <>
              <h4>
                Vendors in {tierName(tierId)}{" "}
                <span className="sub">{excludedInTier} excluded</span>
              </h4>
              <p className="muted">
                Excluded vendors are skipped by the tier; explicit picks below still cover them.
              </p>
              {tierVendors.length > 10 && (
                <button
                  type="button"
                  className="btn-secondary-sm"
                  onClick={() => setShowAllTierVendors(!showAllTierVendors)}
                >
                  {showAllTierVendors ? "Hide vendors" : `Show ${tierVendors.length} vendors`}
                </button>
              )}
              {(tierVendors.length <= 10 || showAllTierVendors) && (
                <ul className="job-tier-vendors">
                  {tierVendors.map((v) => (
                    <li key={v.vendor_id}>
                      <label>
                        <input
                          type="checkbox"
                          checked={excluded.has(v.vendor_id)}
                          onChange={() => toggleExcluded(v.vendor_id)}
                        />{" "}
                        exclude {v.vendor_name}
                      </label>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}

          <h4>Explicit picks</h4>
          <p className="muted">
            {tierId
              ? `Everything in ${tierName(tierId)} is covered when the job fires. Add vendors, products or sources beyond it here.`
              : "Pick vendors, products or sources. A fully ticked vendor or product also covers ones added to it later."}
          </p>
          <input
            type="text"
            className="job-editor-filter"
            placeholder="Filter vendors and products"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          {emptyPicks.length > 0 && (
            <p className="muted">
              Empty picks (no sources left): {emptyPicks.join(", ")} — kept unless you change the tree.
            </p>
          )}
          {catalogError ? (
            <p>
              <span className="error-inline">{catalogError}</span>{" "}
              <button type="button" className="btn-secondary-sm" onClick={onRetryCatalog}>
                Retry
              </button>
            </p>
          ) : catalogLoading ? (
            <p className="sub">Loading sources…</p>
          ) : catalog.length === 0 ? (
            <p className="muted">No sources yet.</p>
          ) : (
            <SourceTreePicker vendors={tree} selected={picks} onChange={setPicks} />
          )}
        </div>

        <p className="muted job-coverage-line">Coverage: {summary}</p>
        {error && <span className="error-inline">{error}</span>}

        <div className="picker-actions">
          <button type="button" className="btn-primary" disabled={busy || catalogLoading} onClick={save}>
            {busy ? "Saving…" : savedId ? "Save" : "Create job"}
          </button>
          <button type="button" className="btn-secondary-sm" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
