# Read-Path Scaling (Docs Browser) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scale the documentation browser to thousands of TOC nodes by virtualizing the sidebar tree (render only the visible window over a flattened row array) and adding a client-side filter (matches + ancestors). Backend unchanged.

**Architecture:** A pure `docsTree.ts` helper flattens the visible tree and computes the filtered view; `DocsBrowser` renders the resulting flat row array through `@tanstack/react-virtual`, with a filter input on top. Article selection, badges, breadcrumb, version overlay, and the removed-pages list are preserved.

**Tech Stack:** React 19 + TypeScript + Vite; `@tanstack/react-virtual`.

## Global Constraints

- Frontend only — no backend/API change. The `browse` response shape (`BrowseResponse`/`BrowseTOCEntry`) is unchanged.
- This project has **no frontend test runner**: verify with `cd frontend && npm run build` (type-check) + `npm run lint` (no new errors vs the existing baseline), plus the live check in Task 3. Keep the tree logic as **pure exported functions** in `docsTree.ts` so it's reviewable in isolation.
- Preserve all existing `DocsBrowser` behavior: first-article auto-select, `selectArticle`→`getArticle`, NEW/UPDATED/REMOVED badges, breadcrumb header, version overlay, removed-pages section.
- Filter is client-side, case-insensitive substring on title; when active show matching nodes **plus their ancestors**, fully expanded; empty filter restores the normal expand/collapse tree.
- Branch `feat/read-path-scaling` (off merged `main`). Run frontend commands from `frontend/`.

---

### Task 1: Add `@tanstack/react-virtual`

**Files:**
- Modify: `frontend/package.json` (+ `package-lock.json`)

- [ ] **Step 1: Install**

Run: `cd frontend && npm install @tanstack/react-virtual@^3.13.0`
Expected: resolves and adds to `dependencies` (a 3.x version compatible with React 19).

- [ ] **Step 2: Verify it imports + build still passes**

Run: `cd frontend && npm run build`
Expected: build succeeds (the dep installs cleanly; nothing imports it yet).

- [ ] **Step 3: Commit**

```bash
git add frontend/package.json frontend/package-lock.json
git commit -m "build(ui): add @tanstack/react-virtual for TOC virtualization"
```

---

### Task 2: `docsTree` helpers + virtualized, filterable `DocsBrowser`

**Files:**
- Create: `frontend/src/components/docsTree.ts`
- Modify: `frontend/src/components/DocsBrowser.tsx`
- Modify: `frontend/src/App.css`

**Interfaces:**
- Produces: `FlatRow { node: BrowseTOCEntry; depth: number; hasChildren: boolean; expanded: boolean }`; `flattenVisible(entries, collapsed: Set<string>) -> FlatRow[]`; `filterVisible(entries, query: string) -> FlatRow[]`.

- [ ] **Step 1: Create the pure tree helpers**

Create `frontend/src/components/docsTree.ts`:
```ts
import type { BrowseTOCEntry } from "../types";

export interface FlatRow {
  node: BrowseTOCEntry;
  depth: number;
  hasChildren: boolean;
  expanded: boolean;
}

/** Depth-first flatten of the visible tree, skipping children of collapsed nodes. */
export function flattenVisible(
  entries: BrowseTOCEntry[],
  collapsed: Set<string>,
): FlatRow[] {
  const rows: FlatRow[] = [];
  const walk = (nodes: BrowseTOCEntry[], depth: number) => {
    for (const n of nodes) {
      const hasChildren = n.children.length > 0;
      const expanded = hasChildren && !collapsed.has(n.id);
      rows.push({ node: n, depth, hasChildren, expanded });
      if (expanded) walk(n.children, depth + 1);
    }
  };
  walk(entries, 0);
  return rows;
}

/**
 * Filtered view: include a node if its title matches (case-insensitive substring)
 * or any descendant matches; ancestors of a match are included and shown expanded.
 * Returns rows in depth-first order. Empty/blank query returns [].
 */
export function filterVisible(
  entries: BrowseTOCEntry[],
  query: string,
): FlatRow[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const rows: FlatRow[] = [];
  const subtree = (n: BrowseTOCEntry, depth: number, out: FlatRow[]): boolean => {
    const selfMatch = n.title.toLowerCase().includes(q);
    const childRows: FlatRow[] = [];
    let anyChild = false;
    for (const c of n.children) {
      anyChild = subtree(c, depth + 1, childRows) || anyChild;
    }
    if (selfMatch || anyChild) {
      out.push({
        node: n,
        depth,
        hasChildren: n.children.length > 0,
        expanded: anyChild, // ancestor of a match is shown expanded
      });
      out.push(...childRows);
      return true;
    }
    return false;
  };
  for (const e of entries) subtree(e, 0, rows);
  return rows;
}
```

- [ ] **Step 2: Rewrite `DocsBrowser.tsx`**

Replace the entire contents of `frontend/src/components/DocsBrowser.tsx` with:
```tsx
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
    next.has(id) ? next.delete(id) : next.add(id);
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
```

- [ ] **Step 3: Add/adjust CSS**

In `frontend/src/App.css`, add styles for the new structure, consistent with the existing petrol-ink / signal-amber design system and the existing `.docs-sidebar`/`.docs-toc-*` rules. The sidebar must give the scroll container a bounded height so virtualization works:

```css
/* Docs sidebar becomes a column: filter on top, scrolling virtualized list, removed below. */
.docs-sidebar { display: flex; flex-direction: column; min-height: 0; }
.docs-filter {
  width: 100%;
  margin-bottom: 10px;
  padding: 7px 10px;
  background: var(--ink-1);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  color: inherit;
  font-family: var(--font-body);
}
.docs-filter:focus { outline: none; border-color: var(--amber); box-shadow: 0 0 0 3px var(--amber-ghost); }
.docs-toc-scroll { flex: 1; min-height: 0; overflow-y: auto; }
.docs-virtual-row { } /* rows are absolutely positioned by the virtualizer wrapper */
.docs-hl { background: var(--amber-ghost); color: var(--amber-hi); border-radius: 2px; }
```
Ensure each `.docs-toc-row` is `ROW_HEIGHT` (32px) tall and does not wrap (e.g. `height: 32px; display: flex; align-items: center; white-space: nowrap; overflow: hidden;`) so the virtualizer's fixed `estimateSize` matches. If the existing `.docs-toc-row` rule differs, update it to a fixed 32px height with the flex/nowrap properties. Keep `.docs-sidebar`'s existing width; just add the flex-column behavior and ensure it (and its parent `.docs-layout`) allow the sidebar a bounded height (the layout already constrains height for the content pane; mirror it for the sidebar so `.docs-toc-scroll` can scroll).

- [ ] **Step 4: Type-check, build, lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds (no type errors); lint introduces no new errors vs the baseline.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/docsTree.ts frontend/src/components/DocsBrowser.tsx frontend/src/App.css
git commit -m "feat(ui): virtualize docs TOC + add filter (scales to thousands of pages)"
```

---

### Task 3: Live verification with a synthetic large source

**Files:** none (verification only).

- [ ] **Step 1: Rebuild the frontend (+ ensure backend up)**

Run: `docker compose up -d --build frontend` and confirm backend/postgres are running (`docker compose ps`).

- [ ] **Step 2: Seed a synthetic large source (~3,000 nodes)**

Run this against the running Postgres (the dev DB), creating a vendor + source + a 3-level TOC with ~3,000 article entries and matching articles so `browse` returns thousands of nodes:
```bash
docker compose exec -T postgres psql -U docextractor -d docextractor <<'SQL'
DO $$
DECLARE
  vid uuid := gen_random_uuid();
  sid uuid := gen_random_uuid();
  chap uuid;
  c int; a int;
BEGIN
  INSERT INTO vendors (id, name, created_at, updated_at) VALUES (vid, 'ScaleVendor', now(), now());
  INSERT INTO documentation_sources (id, vendor_id, name, base_url, status, created_at, updated_at)
    VALUES (sid, vid, 'ScaleSource', 'https://scale.test', 'COMPLETED', now(), now());
  FOR c IN 1..30 LOOP
    chap := gen_random_uuid();
    INSERT INTO toc_entries (id, source_id, title, url, level, sort_order, is_article, parent_id)
      VALUES (chap, sid, 'Chapter '||c, NULL, 0, c*1000, false, NULL);
    FOR a IN 1..100 LOOP
      INSERT INTO toc_entries (id, source_id, title, url, level, sort_order, is_article, parent_id)
        VALUES (gen_random_uuid(), sid, 'Page '||c||'-'||a, 'https://scale.test/'||c||'/'||a, 1, c*1000+a, true, chap);
      INSERT INTO articles (id, source_id, toc_entry_id, title, source_url, content_markdown, sort_order, estimated_tokens, content_size_bytes, extracted_at, created_at)
        VALUES (gen_random_uuid(), sid,
          (SELECT id FROM toc_entries WHERE source_id=sid AND title='Page '||c||'-'||a),
          'Page '||c||'-'||a, 'https://scale.test/'||c||'/'||a, '# Page '||c||'-'||a||E'\n\nbody', c*1000+a, 5, 30, now(), now());
    END LOOP;
  END LOOP;
END $$;
SELECT count(*) AS toc_entries FROM toc_entries WHERE source_id IN (SELECT id FROM documentation_sources WHERE name='ScaleSource');
SQL
```
Expected: prints ~3,030 TOC entries (30 chapters + 3,000 pages).

- [ ] **Step 3: Verify in the browser**

Open http://localhost:3000, select vendor **ScaleVendor** → source **ScaleSource** → the docs browser. Confirm:
- The TOC renders immediately and scrolls smoothly (open DevTools → Elements and confirm only a small window of `.docs-toc-row` nodes is in the DOM, not 3,000).
- Expand/collapse a chapter works.
- Typing in the filter (e.g. `Page 17-4`) narrows to matching pages **with their parent chapter** shown, and the match is highlighted; clearing restores the full tree.
- Selecting a page loads its content on the right.

- [ ] **Step 4: Clean up the synthetic source**

Run:
```bash
docker compose exec -T postgres psql -U docextractor -d docextractor -c \
"DELETE FROM vendors WHERE name='ScaleVendor';"
```
Expected: cascats delete the source, its TOC entries, and articles (FK ON DELETE CASCADE). Confirm the docs browser no longer lists ScaleSource.

---

## Self-Review

**Spec coverage:**
- Virtualized tree via flatten + `@tanstack/react-virtual` → Tasks 1, 2 (`flattenVisible`, `useVirtualizer`).
- Client-side filter (matches + ancestors, expanded, highlighted) → Task 2 (`filterVisible`, `highlight`, filter input); removed list filtered too.
- Pure helpers in `docsTree.ts` for reviewability → Task 2 Step 1.
- Backend unchanged → no backend task.
- Preserve existing behavior (select/badges/breadcrumb/overlay/removed) → Task 2 full-file rewrite keeps them.
- Verification: build + lint + live synthetic-large-source check → Tasks 2, 3.

**Placeholder scan:** No TBD/TODO. Task 1's version is `^3.13.0` (a concrete 3.x floor; the implementer commits the resolved lockfile). Task 2 Step 3 names the exact CSS selectors/values and the one behavioral requirement (row = 32px fixed height to match `estimateSize`); the "if the existing rule differs, update it" instruction is concrete (fixed height + flex + nowrap).

**Type consistency:** `FlatRow` and `flattenVisible`/`filterVisible` signatures match between `docsTree.ts` and `DocsBrowser.tsx`; `ROW_HEIGHT=32` matches the CSS row height and the virtualizer `estimateSize`; `useVirtualizer`'s `getScrollElement` returns the `scrollRef` element which has `overflow-y:auto` (`.docs-toc-scroll`).

## Out of scope (from the spec)
Backend pagination / lazy children-on-expand; virtualizing the changelog/article list; server-side TOC search; persisting expand/collapse or filter state.
