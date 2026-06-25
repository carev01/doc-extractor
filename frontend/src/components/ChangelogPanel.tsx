import { useState, useEffect, useMemo, Fragment } from "react";
import type { DocumentationSource, ChangelogEntry, ArticleDetail } from "../types";
import { getSourceChangelog, getArticle } from "../api/client";
import MarkdownView from "./MarkdownView";
import VersionOverlay from "./VersionOverlay";

interface Props {
  source: DocumentationSource;
}

const BADGE: Record<ChangelogEntry["change_type"], string> = {
  initial: "INITIAL",
  added: "ADDED",
  changed: "CHANGED",
  removed: "REMOVED",
};

function dateKey(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export default function ChangelogPanel({ source }: Props) {
  const [entries, setEntries] = useState<ChangelogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // Open viewer: either a side-by-side version overlay (changed) or a rendered
  // article (added/removed).
  const [overlay, setOverlay] = useState<{ id: string; title: string; md: string } | null>(null);
  const [article, setArticle] = useState<{ detail: ArticleDetail; removed: boolean } | null>(null);

  useEffect(() => {
    setOverlay(null);
    setArticle(null);
    setLoading(true);
    setError("");
    getSourceChangelog(source.id)
      .then((d) => setEntries(d.entries))
      .catch(() => setError("Failed to load changelog"))
      .finally(() => setLoading(false));
  }, [source.id]);

  const groups = useMemo(() => {
    const m = new Map<string, ChangelogEntry[]>();
    for (const e of entries) {
      const k = dateKey(e.timestamp);
      const bucket = m.get(k);
      if (bucket) bucket.push(e);
      else m.set(k, [e]);
    }
    return Array.from(m.entries()); // insertion order = newest-first from API
  }, [entries]);

  // Flat newest-first index: maps each entry (by its position in the flat
  // sequence) to the version of the immediately-preceding rendered entry.
  // Used to detect cross-group version boundaries without mutable iteration
  // inside JSX.
  const prevVersionByFlatIdx = useMemo(() => {
    const flat = groups.flatMap(([, evs]) => evs);
    const result: (string | null)[] = [];
    let lastVersion: string | null = null;
    for (const e of flat) {
      result.push(lastVersion);
      lastVersion = e.version;   // update on every entry, incl. null, so nulls break the chain
    }
    return result;
  }, [groups]);

  const openEntry = async (e: ChangelogEntry) => {
    if (!e.article_id) return; // 'initial' summary has no article to open
    setError("");
    try {
      const detail = await getArticle(e.article_id);
      if (e.change_type === "changed") {
        setOverlay({ id: e.article_id, title: detail.title, md: detail.content_markdown });
      } else {
        setArticle({ detail, removed: e.change_type === "removed" });
      }
    } catch {
      setError("Failed to open article");
    }
  };

  if (source.status !== "completed") {
    return (
      <div className="changelog-panel">
        <p className="hint">
          Run an extraction first — the changelog records changes captured across runs.
        </p>
      </div>
    );
  }

  return (
    <div className="changelog-panel">
      <h2>Changelog — {source.name}</h2>
      <p className="hint">A timeline of page additions, changes and removals, newest first.</p>

      {error && <p className="error">{error}</p>}
      {loading && <p>Loading changelog…</p>}
      {!loading && entries.length === 0 && <p className="hint">No events yet.</p>}

      {groups.map(([day, evs], gi) => {
        const flatOffset = groups.slice(0, gi).reduce((n, [, ev]) => n + ev.length, 0);
        return (
          <div key={day} className="timeline-group">
            <div className="timeline-date">{day}</div>
            <ul className="timeline-list">
              {evs.map((e, i) => {
                const flatIdx = flatOffset + i;
                const prevV = prevVersionByFlatIdx[flatIdx];
                const showBoundary =
                  e.version !== null && prevV !== null && e.version !== prevV;
                return (
                  <Fragment key={`row-${flatIdx}`}>
                    {showBoundary && (
                      <li key={`vb-${flatIdx}`} className="version-boundary">
                        {prevV} → {e.version}
                      </li>
                    )}
                    <li
                      key={`${e.change_type}-${e.version_id ?? e.article_id ?? "x"}-${i}`}
                      className="timeline-row"
                    >
                      {e.change_type === "initial" ? (
                        <div className="timeline-event timeline-initial">
                          <span className="badge-initial">{BADGE.initial}</span>
                          <span className="timeline-title">{e.title}</span>
                          {e.version !== null && <span className="version-tag">v{e.version}</span>}
                        </div>
                      ) : (
                        <button className="timeline-event" onClick={() => openEntry(e)}>
                          <span className={`badge-${e.change_type}`}>{BADGE[e.change_type]}</span>
                          <span className="timeline-title">{e.title}</span>
                          {e.version !== null && <span className="version-tag">v{e.version}</span>}
                        </button>
                      )}
                    </li>
                  </Fragment>
                );
              })}
            </ul>
          </div>
        );
      })}

      {overlay && (
        <VersionOverlay
          articleId={overlay.id}
          title={overlay.title}
          currentMarkdown={overlay.md}
          onClose={() => setOverlay(null)}
        />
      )}

      {article && (
        <div className="article-modal-backdrop" onClick={() => setArticle(null)}>
          <div className="article-modal" onClick={(ev) => ev.stopPropagation()}>
            <div className="article-modal-head">
              <h3>{article.detail.title}</h3>
              <button onClick={() => setArticle(null)}>✕</button>
            </div>
            {article.removed && (
              <div className="removed-banner">
                This page is no longer present in the source's current table of
                contents. It is preserved here from the last run that included it.
              </div>
            )}
            <MarkdownView content={article.detail.content_markdown} />
          </div>
        </div>
      )}
    </div>
  );
}
