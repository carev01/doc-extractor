import { useState, useEffect, useMemo, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type {
  DocumentationSource,
  BrowseResponse,
  BrowseTOCEntry,
  ArticleDetail,
} from "../types";
import { browseSource, getArticle } from "../api/client";
import { flattenVisible, filterVisible, type FlatRow } from "./docsTree";
import MarkdownView from "./MarkdownView";
import VersionOverlay from "./VersionOverlay";

interface Props {
  source: DocumentationSource;
}

interface ArticleMeta {
  title: string;
  change_status: "new" | "updated" | "unchanged" | null;
  version_count: number;
  removed: boolean;
}

const ROW_HEIGHT = 32;

function firstArticleId(nodes: BrowseTOCEntry[]): string | null {
  for (const n of nodes) {
    if (n.article_id) return n.article_id;
    const child = firstArticleId(n.children);
    if (child) return child;
  }
  return null;
}

/** Wrap the matched substring in <mark> (case-insensitive). */
function highlight(title: string, query: string) {
  const q = query.trim();
  if (!q) return title;
  const idx = title.toLowerCase().indexOf(q.toLowerCase());
  if (idx < 0) return title;
  return (
    <>
      {title.slice(0, idx)}
      <mark className="docs-hl">{title.slice(idx, idx + q.length)}</mark>
      {title.slice(idx + q.length)}
    </>
  );
}

export default function DocsBrowser({ source }: Props) {
  const [data, setData] = useState<BrowseResponse | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [article, setArticle] = useState<ArticleDetail | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const [loadingArticle, setLoadingArticle] = useState(false);
  const [error, setError] = useState("");

  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (source.status === "completed") load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source.id, source.status]);

  const load = async () => {
    setError("");
    setData(null);
    setArticle(null);
    setSelectedId(null);
    setQuery("");
    try {
      const d = await browseSource(source.id);
      setData(d);
      const first = firstArticleId(d.entries);
      if (first) selectArticle(first);
    } catch {
      setError("Failed to load documentation");
    }
  };

  const metaById = useMemo(() => {
    const m = new Map<string, ArticleMeta>();
    if (!data) return m;
    const walk = (nodes: BrowseTOCEntry[]) => {
      for (const n of nodes) {
        if (n.article_id) {
          m.set(n.article_id, {
            title: n.title,
            change_status: n.change_status,
            version_count: n.version_count,
            removed: false,
          });
        }
        walk(n.children);
      }
    };
    walk(data.entries);
    for (const r of data.removed) {
      m.set(r.article_id, {
        title: r.title,
        change_status: null,
        version_count: r.version_count,
        removed: true,
      });
    }
    return m;
  }, [data]);

  // The flat row array fed to the virtualizer: filtered view when querying,
  // otherwise the expand/collapse-aware flatten.
  const rows: FlatRow[] = useMemo(() => {
    if (!data) return [];
    return query.trim()
      ? filterVisible(data.entries, query)
      : flattenVisible(data.entries, collapsed);
  }, [data, collapsed, query]);

  // @tanstack/react-virtual's useVirtualizer is not yet React-Compiler-compatible;
  // it opts this component out of compilation but is otherwise correct.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });

  const selectArticle = async (articleId: string) => {
    setSelectedId(articleId);
    setShowHistory(false);
    setArticle(null);
    setLoadingArticle(true);
    try {
      setArticle(await getArticle(articleId));
    } catch {
      setError("Failed to load article");
    } finally {
      setLoadingArticle(false);
    }
  };

  const toggle = (id: string) => {
    const next = new Set(collapsed);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setCollapsed(next);
  };

  const renderBadge = (status: ArticleMeta["change_status"]) => {
    if (status === "new") return <span className="badge-new">NEW</span>;
    if (status === "updated") return <span className="badge-upd">UPDATED</span>;
    return null;
  };

  const removed = useMemo(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    return q
      ? data.removed.filter((r) => r.title.toLowerCase().includes(q))
      : data.removed;
  }, [data, query]);

  if (source.status !== "completed") {
    return (
      <div className="docs-browser">
        <p className="hint">
          Run an extraction first — the browser renders the stored documentation.
        </p>
      </div>
    );
  }

  const renderRow = (row: FlatRow) => {
    const n = row.node;
    const isArticle = !!n.article_id;
    return (
      <div
        className="docs-toc-row"
        style={{ paddingLeft: row.depth * 14 }}
      >
        {row.hasChildren ? (
          <button className="docs-toc-caret" onClick={() => toggle(n.id)}>
            {row.expanded ? "▾" : "▸"}
          </button>
        ) : (
          <span className="docs-toc-caret-spacer" />
        )}
        {isArticle ? (
          <button
            className={`docs-toc-link ${selectedId === n.article_id ? "active" : ""}`}
            onClick={() => selectArticle(n.article_id!)}
          >
            <span className="docs-toc-title">{highlight(n.title, query)}</span>
            {renderBadge(n.change_status)}
          </button>
        ) : (
          <button className="docs-toc-section" onClick={() => toggle(n.id)}>
            {highlight(n.title, query)}
          </button>
        )}
      </div>
    );
  };

  const meta = article ? metaById.get(article.id) : undefined;

  return (
    <div className="docs-browser">
      {error && <p className="error">{error}</p>}

      <div className="docs-layout">
        <nav className="docs-sidebar">
          <input
            className="docs-filter"
            type="search"
            placeholder="Filter pages…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />

          {data && rows.length === 0 && (
            <p className="hint">
              {query.trim() ? "No pages match the filter." : "No pages extracted."}
            </p>
          )}

          <div ref={scrollRef} className="docs-toc-scroll">
            <div
              style={{
                height: virtualizer.getTotalSize(),
                position: "relative",
                width: "100%",
              }}
            >
              {virtualizer.getVirtualItems().map((vi) => (
                <div
                  key={rows[vi.index].node.id}
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    height: vi.size,
                    transform: `translateY(${vi.start}px)`,
                  }}
                >
                  {renderRow(rows[vi.index])}
                </div>
              ))}
            </div>
          </div>

          {removed.length > 0 && (
            <div className="docs-removed">
              <div className="docs-removed-label">Removed pages</div>
              <ul className="docs-toc-list">
                {removed.map((r) => (
                  <li key={r.article_id} className="docs-toc-item">
                    <div className="docs-toc-row">
                      <span className="docs-toc-caret-spacer" />
                      <button
                        className={`docs-toc-link removed ${
                          selectedId === r.article_id ? "active" : ""
                        }`}
                        onClick={() => selectArticle(r.article_id)}
                      >
                        <span className="docs-toc-title">{highlight(r.title, query)}</span>
                        <span className="badge-removed">REMOVED</span>
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </nav>

        <section className="docs-content">
          {loadingArticle && <p>Loading…</p>}
          {!loadingArticle && !article && (
            <p className="hint">Select a page from the table of contents.</p>
          )}
          {article && (
            <>
              {meta?.removed && (
                <div className="removed-banner">
                  This page is no longer present in the source's current table of
                  contents. It is preserved here from the last run that included it.
                </div>
              )}
              <div className="docs-content-head">
                <Breadcrumb article={article} />
                <h2>
                  {article.title} {renderBadge(meta?.change_status ?? null)}
                </h2>
                <div className="docs-content-meta">
                  <a href={article.source_url} target="_blank" rel="noopener noreferrer">
                    {article.source_url}
                  </a>
                  {article.last_updated_at && (
                    <span>
                      Source updated{" "}
                      {new Date(article.last_updated_at).toLocaleDateString()}
                    </span>
                  )}
                  <span>
                    Last scraped {new Date(article.extracted_at).toLocaleDateString()}
                  </span>
                  {meta && meta.version_count > 0 && (
                    <button className="btn-link" onClick={() => setShowHistory(true)}>
                      History ({meta.version_count})
                    </button>
                  )}
                </div>
              </div>
              <MarkdownView content={article.content_markdown} />
            </>
          )}
        </section>
      </div>

      {showHistory && article && (
        <VersionOverlay
          articleId={article.id}
          title={article.title}
          currentMarkdown={article.content_markdown}
          onClose={() => setShowHistory(false)}
        />
      )}
    </div>
  );
}

/** Vendor / product / chapter trail above the article title. */
function Breadcrumb({ article }: { article: ArticleDetail }) {
  const parts: string[] = [];
  if (article.vendor) parts.push(article.vendor.name);
  if (article.product) parts.push(article.product.name);
  if (article.top_level_chapter) parts.push(article.top_level_chapter.title);
  if (
    article.parent_chapter &&
    article.parent_chapter.id !== article.top_level_chapter?.id
  ) {
    parts.push(article.parent_chapter.title);
  }
  if (parts.length === 0) return null;

  return (
    <div className="docs-breadcrumb">
      {parts.map((p, i) => (
        <span key={i}>
          {i > 0 && <span className="docs-breadcrumb-sep">›</span>}
          {p}
        </span>
      ))}
    </div>
  );
}
