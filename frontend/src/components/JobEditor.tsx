import { useEffect, useMemo, useState } from "react";
import type { CatalogVendor, Job, JobSchedule, ScheduleKind, Tier } from "../types";
import { createJob, previewJobSchedule, setJobCoverage, updateJob } from "../api/client";
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
export default function JobEditor({ job, jobs, tiers, catalog, onClose, onSaved }: Props) {
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

  const [repeat, setRepeat] = useState<Repeat>(repeatOf(job?.schedule_kind ?? null));
  const [monthlyOn, setMonthlyOn] = useState<MonthlyOn>(
    job?.schedule_kind === "monthly_nth" ? (job.nth ?? "first") : "day",
  );
  const [interval, setIntervalMonths] = useState(job?.interval_months ?? 1);
  // Matches the backend default (next month) so the preview and the save agree.
  const [startMonth, setStartMonth] = useState(job?.start_month ?? (new Date().getMonth() + 1) % 12 + 1);
  const [dayOfMonth, setDayOfMonth] = useState(job?.day_of_month ?? 1);
  const [weekday, setWeekday] = useState(job?.weekday ?? 0);
  const [time, setTime] = useState(job?.time_of_day ?? "02:00");
  const [tz, setTz] = useState(job?.timezone ?? browserTz);

  const [preview, setPreview] = useState<string[]>([]);
  const [previewError, setPreviewError] = useState("");
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

  // Live "Next runs": debounced so typing a timezone doesn't fire per keystroke.
  useEffect(() => {
    if (!schedule.schedule_kind) return;
    let cancelled = false;
    const { schedule_kind, ...rest } = schedule;
    const t = window.setTimeout(() => {
      previewJobSchedule({ ...rest, kind: schedule_kind })
        .then((runs) => {
          if (cancelled) return;
          setPreview(runs);
          setPreviewError("");
        })
        .catch((e) => {
          if (cancelled) return;
          setPreview([]);
          setPreviewError(apiError(e, "Could not preview this schedule"));
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
    const covered = resolveJob(tierId, picks, idx);
    const ov = overlapWith(covered, savedId, jobs, idx);
    return coverageLine(
      {
        sources: covered.size,
        products: new Set([...covered].map((s) => idx.productOf.get(s))).size,
        overlap_sources: ov.count,
        login_sources: [...covered].filter((s) => idx.login.has(s)).length,
      },
      ov.names,
    );
  }, [tierId, picks, idx, savedId, jobs]);

  const picksDirty =
    picks.size !== initialPicks.size || [...picks].some((s) => !initialPicks.has(s));

  const save = async () => {
    if (!name.trim()) {
      setError("A job needs a name");
      return;
    }
    setBusy(true);
    setError("");
    const body = { name: name.trim(), enabled: enabled && kind !== null, tier_id: tierId, ...schedule };
    try {
      let id = savedId;
      if (id) {
        await updateJob(id, body);
      } else {
        id = (await createJob(body)).id;
        setSavedId(id);
      }
      // Only rewrite the picks when they changed: an untouched tree must not
      // replace a dynamic target whose sources are all gone (nothing to tick).
      if (picksDirty || (!job && picks.size > 0)) await setJobCoverage(id, [...picks]);
      onSaved();
      onClose();
    } catch (e) {
      setError(apiError(e, "Failed to save the job"));
      onSaved();
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
                    <select value={startMonth} onChange={(e) => setStartMonth(Number(e.target.value))}>
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
              {previewError ? (
                <span className="error-inline">{previewError}</span>
              ) : (
                <ul>
                  {preview.map((r) => (
                    <li key={r} className="sub">
                      {fmtInTz(r, tz)}
                    </li>
                  ))}
                </ul>
              )}
            </div>
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
          {catalog.length === 0 ? (
            <p className="muted">No sources yet.</p>
          ) : (
            <SourceTreePicker vendors={tree} selected={picks} onChange={setPicks} />
          )}
        </div>

        <p className="muted job-coverage-line">Coverage: {summary}</p>
        {error && <span className="error-inline">{error}</span>}

        <div className="picker-actions">
          <button type="button" className="btn-primary" disabled={busy} onClick={save}>
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
