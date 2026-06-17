import { useState, useEffect } from "react";
import type { ArticleVersion, ArticleVersionDetail, VersionDiff } from "../types";
import {
  listArticleVersions,
  getArticleVersion,
  getVersionDiff,
} from "../api/client";
import MarkdownView from "./MarkdownView";
import DiffView from "./DiffView";

interface Props {
  articleId: string;
  title: string;
  currentMarkdown: string;
  onClose: () => void;
}

type Mode = "side-by-side" | "diff";

export default function VersionOverlay({
  articleId,
  title,
  currentMarkdown,
  onClose,
}: Props) {
  const [versions, setVersions] = useState<ArticleVersion[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [versionDetail, setVersionDetail] =
    useState<ArticleVersionDetail | null>(null);
  const [diff, setDiff] = useState<VersionDiff | null>(null);
  const [mode, setMode] = useState<Mode>("side-by-side");
  const [error, setError] = useState("");

  useEffect(() => {
    listArticleVersions(articleId)
      .then((data) => {
        setVersions(data.versions);
        if (data.versions.length > 0) {
          selectVersion(data.versions[0].id);
        }
      })
      .catch(() => setError("Failed to load version history"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [articleId]);

  const selectVersion = async (versionId: string) => {
    setSelectedId(versionId);
    setVersionDetail(null);
    setDiff(null);
    try {
      const [detail, d] = await Promise.all([
        getArticleVersion(articleId, versionId),
        getVersionDiff(articleId, versionId, "current"),
      ]);
      setVersionDetail(detail);
      setDiff(d);
    } catch {
      setError("Failed to load version");
    }
  };

  return (
    <div className="overlay-backdrop" onClick={onClose}>
      <div className="overlay-panel" onClick={(e) => e.stopPropagation()}>
        <header className="overlay-header">
          <h3>History — {title}</h3>
          <button className="overlay-close" onClick={onClose}>
            ✕
          </button>
        </header>

        {error && <p className="error">{error}</p>}

        <div className="overlay-body">
          <aside className="version-list">
            <div className="version-list-label">Previous versions</div>
            {versions.length === 0 && (
              <p className="hint">No prior versions recorded.</p>
            )}
            <ul>
              {versions.map((v) => (
                <li key={v.id}>
                  <button
                    className={selectedId === v.id ? "active" : ""}
                    onClick={() => selectVersion(v.id)}
                  >
                    {new Date(v.extracted_at).toLocaleString()}
                  </button>
                </li>
              ))}
            </ul>
          </aside>

          <section className="version-compare">
            <div className="version-toolbar">
              <button
                className={mode === "side-by-side" ? "active" : ""}
                onClick={() => setMode("side-by-side")}
              >
                Side by side
              </button>
              <button
                className={mode === "diff" ? "active" : ""}
                onClick={() => setMode("diff")}
              >
                Highlighted changes
              </button>
            </div>

            {selectedId === null && (
              <p className="hint">Select a version to compare with current.</p>
            )}

            {mode === "side-by-side" && versionDetail && (
              <div className="side-by-side">
                <div className="version-col">
                  <div className="version-col-label">
                    Previous ·{" "}
                    {new Date(versionDetail.extracted_at).toLocaleString()}
                  </div>
                  <MarkdownView content={versionDetail.content_markdown} />
                </div>
                <div className="version-col">
                  <div className="version-col-label">Current</div>
                  <MarkdownView content={currentMarkdown} />
                </div>
              </div>
            )}

            {mode === "diff" && diff && <DiffView text={diff.diff_text} />}
          </section>
        </div>
      </div>
    </div>
  );
}
