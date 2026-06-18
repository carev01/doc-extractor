import { useEffect, useState } from "react";
import type { DocumentationSource, Frequency, Schedule, ScheduleConfig } from "../types";
import { getSchedule, putSchedule } from "../api/client";

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export default function ScheduleControl({ source }: { source: DocumentationSource }) {
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const [cfg, setCfg] = useState<ScheduleConfig>({
    enabled: false, frequency: "daily", time_of_day: "02:00",
    day_of_week: 0, day_of_month: 1, timezone: browserTz,
  });
  const [nextRun, setNextRun] = useState<string | null>(null);
  const [lastRun, setLastRun] = useState<Schedule["last_run"]>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    getSchedule(source.id).then((s) => {
      if (s) {
        setCfg({
          enabled: s.enabled, frequency: s.frequency as Frequency,
          time_of_day: s.time_of_day, day_of_week: s.day_of_week ?? 0,
          day_of_month: s.day_of_month ?? 1, timezone: s.timezone,
        });
        setNextRun(s.next_run_at);
        setLastRun(s.last_run);
      }
    }).catch(() => setError("Failed to load schedule"));
  }, [source.id]);

  const save = async () => {
    setSaving(true); setError("");
    try {
      const s = await putSchedule(source.id, cfg);
      setNextRun(s.next_run_at);
    } catch {
      setError("Failed to save schedule");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="schedule-control">
      <label className="schedule-toggle">
        <input
          type="checkbox"
          checked={cfg.enabled}
          onChange={(e) => setCfg({ ...cfg, enabled: e.target.checked })}
        />
        Run on a schedule
      </label>

      {cfg.enabled && (
        <div className="schedule-fields">
          <select
            value={cfg.frequency}
            onChange={(e) => setCfg({ ...cfg, frequency: e.target.value as Frequency })}
          >
            <option value="hourly">Hourly</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
          </select>

          {cfg.frequency === "weekly" && (
            <select
              value={cfg.day_of_week ?? 0}
              onChange={(e) => setCfg({ ...cfg, day_of_week: Number(e.target.value) })}
            >
              {DAYS.map((d, i) => <option key={i} value={i}>{d}</option>)}
            </select>
          )}

          {cfg.frequency === "monthly" && (
            <select
              value={cfg.day_of_month ?? 1}
              onChange={(e) => setCfg({ ...cfg, day_of_month: Number(e.target.value) })}
            >
              {Array.from({ length: 28 }, (_, i) => i + 1).map((d) =>
                <option key={d} value={d}>{d}</option>)}
            </select>
          )}

          {cfg.frequency !== "hourly" && (
            <input
              type="time"
              value={cfg.time_of_day}
              onChange={(e) => setCfg({ ...cfg, time_of_day: e.target.value })}
            />
          )}
          {cfg.frequency === "hourly" && (
            <span className="hint">at minute {cfg.time_of_day.split(":")[1]}</span>
          )}

          <span className="schedule-tz">{cfg.timezone}</span>
        </div>
      )}

      <button onClick={save} disabled={saving}>
        {saving ? "Saving…" : "Save schedule"}
      </button>
      {nextRun && cfg.enabled && (
        <p className="hint">Next run: {new Date(nextRun).toLocaleString()}</p>
      )}
      {lastRun && (
        <p className="hint">Last run: {lastRun.status}</p>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
