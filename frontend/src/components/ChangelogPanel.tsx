import { useState, useEffect } from "react";
import type {
  DocumentationSource,
  ChangelogEntry,
  VersionDiff,
} from "../types";
import { getSourceChangelog, getVersionDiff } from "../api/client";
import DiffView from "./DiffView";

interface Props {
  source: DocumentationSource;
}

type Against = "next" | "current";

export default function ChangelogPanel({ source }: Props) {
  const [entries, setEntries] = useState<ChangelogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [openVersionId, setOpenVersionId] = useState<string | null>(null);
  const [against, setAgainst] = useState<Against>("next");
  const [diff, setDiff] = useState<VersionDiff | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);

  useEffect(() => {
    loadChangelog();
    setOpenVersionId(null);
    setDiff(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source.id]);

  const loadChangelog = async () => {
    setLoading(true);
    setError("");
    try {
      const data = await getSourceChangelog(source.id);
      setEntries(data.entries);
    } catch (e) {
      setError("Failed to load changelog");
    } finally {
      setLoading(false);
    }
  };

  const loadDiff = async (entry: ChangelogEntry, mode: Against) => {
    setDiffLoading(true);
    setDiff(null);
    try {
      const data = await getVersionDiff(entry.article_id, entry.version_id, mode);
      setDiff(data);
    } catch (e) {
      setError("Failed to load diff");
    } finally {
      setDiffLoading(false);
    }
  };

  const handleToggleEntry = (entry: ChangelogEntry) => {
    if (openVersionId === entry.version_id) {
      setOpenVersionId(null);
      setDiff(null);
      return;
    }
    setOpenVersionId(entry.version_id);
    setAgainst("next");
    loadDiff(entry, "next");
  };

  const handleSetAgainst = (entry: ChangelogEntry, mode: Against) => {
    setAgainst(mode);
    loadDiff(entry, mode);
  };

  if (source.status !== "completed") {
    return (
      <div className="changelog-panel">
        <p className="hint">
          Run an extraction first — the changelog records changes captured
          across runs.
        </p>
      </div>
    );
  }

  return (
    <div className="changelog-panel">
      <h2>Changelog — {source.name}</h2>
      <p className="hint">
        Every recorded article change, newest first. Expand an entry to see what
        changed.
      </p>

      {error && <p className="error">{error}</p>}
      {loading && <p>Loading changelog…</p>}

      {!loading && entries.length === 0 && (
        <p className="hint">
          No changes recorded yet. Changes appear here after a re-run detects
          updated content.
        </p>
      )}

      <ul className="changelog-list">
        {entries.map((entry) => {
          const open = openVersionId === entry.version_id;
          return (
            <li key={entry.version_id} className="changelog-entry">
              <button
                className="changelog-entry-header"
                onClick={() => handleToggleEntry(entry)}
              >
                <span className="changelog-caret">{open ? "▾" : "▸"}</span>
                <span className="changelog-title">{entry.title}</span>
                <span className="changelog-date">
                  {new Date(entry.extracted_at).toLocaleString()}
                </span>
                {!entry.has_diff && (
                  <span className="status-badge">computed diff</span>
                )}
              </button>

              {open && (
                <div className="diff-container">
                  <div className="diff-toolbar">
                    <button
                      className={against === "next" ? "active" : ""}
                      onClick={() => handleSetAgainst(entry, "next")}
                    >
                      vs. next version
                    </button>
                    <button
                      className={against === "current" ? "active" : ""}
                      onClick={() => handleSetAgainst(entry, "current")}
                    >
                      vs. current
                    </button>
                  </div>
                  {diffLoading && <p>Loading diff…</p>}
                  {!diffLoading && diff && <DiffView text={diff.diff_text} />}
                  {!diffLoading && diff && diff.diff_text.trim() === "" && (
                    <p className="hint">No textual differences.</p>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
