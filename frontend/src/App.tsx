import { lazy, Suspense, useEffect, useRef, useState } from "react";
import type { Vendor, Product, DocumentationSource, AuthUser } from "./types";
import { authApi, getAccessToken } from "./api/client";
import { accessFrom, OPEN_ACCESS, type Access } from "./access";
import { Login } from "./views/Login";
import VendorList from "./components/VendorList";
import "./App.css";

// Code-split the non-initial views: the authed shell opens on the Vendors list,
// so everything else — including the heavy markdown/diff path (DocsBrowser,
// ChangelogPanel, ExportPanel) — is loaded on demand, keeping the initial bundle
// to the shell + vendor list.
const ProductList = lazy(() => import("./components/ProductList"));
const SourceList = lazy(() => import("./components/SourceList"));
const JobsView = lazy(() => import("./components/JobsView"));
const Dashboard = lazy(() => import("./components/Dashboard"));
const ExportPanel = lazy(() => import("./components/ExportPanel"));
const ChangelogPanel = lazy(() => import("./components/ChangelogPanel"));
const DocsBrowser = lazy(() => import("./components/DocsBrowser"));
const ApiKeys = lazy(() => import("./views/ApiKeys").then((m) => ({ default: m.ApiKeys })));
const Admin = lazy(() => import("./views/Admin").then((m) => ({ default: m.Admin })));
const Account = lazy(() => import("./views/Account").then((m) => ({ default: m.Account })));
const Logins = lazy(() => import("./views/Logins").then((m) => ({ default: m.Logins })));
const Webhooks = lazy(() => import("./views/Webhooks").then((m) => ({ default: m.Webhooks })));
const Exports = lazy(() => import("./views/Exports").then((m) => ({ default: m.Exports })));

type View =
  | "vendors"
  | "products"
  | "sources"
  | "browse"
  | "export"
  | "changelog"
  | "jobs"
  | "dashboard"
  | "exports"
  | "logins"
  | "webhooks"
  | "apikeys"
  | "user-management"
  | "account";
const SOURCE_TABS = ["browse", "export", "changelog"] as const;
const SOURCE_TAB_LABELS: Record<string, string> = {
  browse: "Browse",
  export: "Export",
  changelog: "Changelog",
};

/** Views backed by an admin-only router; a non-admin can't load these at all. */
const ADMIN_ONLY_VIEWS = new Set<View>(["jobs", "logins", "user-management"]);

export default function App() {
  const [selectedView, setView] = useState<View>("vendors");
  const [selectedVendor, setSelectedVendor] = useState<Vendor | null>(null);
  const [selectedProduct, setSelectedProduct] = useState<Product | null>(null);
  const [selectedSource, setSelectedSource] =
    useState<DocumentationSource | null>(null);

  // Auth gate. 'loading' = probing; 'open' = auth disabled on the server;
  // 'login' = sign-in required; 'authed' = signed in.
  const [authGate, setAuthGate] = useState<"loading" | "open" | "login" | "authed">("loading");
  const [needsBootstrap, setNeedsBootstrap] = useState(false);
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);
  // Which controls to render. Defaults to open so the UI behaves as it did
  // before gating existed if /auth/my-access can't be reached; the server
  // enforces regardless.
  const [access, setAccess] = useState<Access>(OPEN_ACCESS);

  // A non-admin can be holding an admin-only view — access resolves after the
  // first render, and the menu item may have been clicked before that. Derived
  // during render instead of corrected in an effect, which would cascade an
  // extra render and flash an empty main area.
  const view: View =
    !access.isAdmin && ADMIN_ONLY_VIEWS.has(selectedView) ? "vendors" : selectedView;

  // Hamburger menu state
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await authApi.status();
        if (cancelled) return;
        if (!status.auth_enabled) {
          setAuthGate("open");
          return;
        }
        setNeedsBootstrap(status.needs_bootstrap);
        if (!getAccessToken()) {
          setAuthGate("login");
          return;
        }
        try {
          const u = await authApi.me();
          setCurrentUser(u);
          try {
            setAccess(accessFrom(await authApi.myAccess()));
          } catch {
            // Older backend without the endpoint — leave controls as they were.
            setAccess(OPEN_ACCESS);
          }
          setAuthGate("authed");
        } catch {
          setAuthGate("login");
        }
      } catch {
        // Status probe failed (e.g. older backend without /auth/status) — don't
        // hard-block the UI.
        if (!cancelled) setAuthGate("open");
      }
    })();
    const onExpired = () => setAuthGate("login");
    window.addEventListener("dx-auth-expired", onExpired);
    return () => {
      cancelled = true;
      window.removeEventListener("dx-auth-expired", onExpired);
    };
  }, []);

  // Click-outside handler for hamburger menu
  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [menuOpen]);

  const handleLogout = () => {
    authApi.logout();
    setAccess(OPEN_ACCESS);
    setCurrentUser(null);
    setAuthGate("login");
  };

  const handleSelectVendor = (vendor: Vendor) => {
    setSelectedVendor(vendor);
    setSelectedProduct(null);
    setSelectedSource(null);
    setView("products");
  };

  const handleSelectProduct = (product: Product) => {
    setSelectedProduct(product);
    setSelectedSource(null);
    setView("sources");
  };

  const handleSelectSource = (source: DocumentationSource) => {
    setSelectedSource(source);
    setView("browse");
  };

  const handleMenuNav = (target: View) => {
    setView(target);
    setMenuOpen(false);
  };

  if (authGate === "loading") {
    return <div className="app" style={{ padding: "2em" }}>Loading…</div>;
  }
  if (authGate === "login") {
    return (
      <Login
        needsBootstrap={needsBootstrap}
        onSuccess={async () => {
          try {
            setCurrentUser(await authApi.me());
          } catch {
            /* ignore — /me will be retried on next load */
          }
          setAuthGate("authed");
        }}
      />
    );
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">◧</span>
          <div className="brand-text">
            <h1 className="wordmark">DocExtractor</h1>
            <p className="brand-tagline">
              Capture, preserve &amp; track product documentation
            </p>
          </div>
        </div>
        <nav className="breadcrumb">
          <button
            className={view === "vendors" ? "active" : ""}
            onClick={() => {
              setView("vendors");
              setSelectedVendor(null);
              setSelectedProduct(null);
              setSelectedSource(null);
            }}
          >
            Vendors
          </button>
          {selectedVendor && (
            <>
              <span className="sep">/</span>
              <button
                className={view === "products" ? "active" : ""}
                onClick={() => {
                  setView("products");
                  setSelectedProduct(null);
                  setSelectedSource(null);
                }}
              >
                {selectedVendor.name}
              </button>
            </>
          )}
          {selectedProduct && (
            <>
              <span className="sep">/</span>
              <button
                className={view === "sources" ? "active" : ""}
                onClick={() => {
                  setView("sources");
                  setSelectedSource(null);
                }}
              >
                {selectedProduct.name}
              </button>
            </>
          )}
          {selectedSource && (
            <>
              <span className="sep">/</span>
              <button className="active">{selectedSource.name}</button>
            </>
          )}
          <span className="sep">│</span>
          {/* Jobs and Logins are admin-only routers (/api/jobs, /api/auth-realms
              are gated at the router level), so for a non-admin the views can
              only ever 403 — hide them rather than offer a dead end. */}
          {access.isAdmin && (
            <button
              className={view === "jobs" ? "active" : ""}
              onClick={() => setView("jobs")}
            >
              Jobs
            </button>
          )}
          <button
            className={view === "dashboard" ? "active" : ""}
            onClick={() => setView("dashboard")}
          >
            Dashboard
          </button>
          <div className="hamburger-wrapper" ref={menuRef}>
            <button
              className={`hamburger${menuOpen ? " active" : ""}`}
              onClick={() => setMenuOpen((v) => !v)}
              aria-label="Menu"
              aria-expanded={menuOpen}
            >
              ☰
            </button>
            {menuOpen && (
              <div className="hamburger-menu">
                <button
                  className={`hamburger-menu-item${view === "exports" ? " active" : ""}`}
                  onClick={() => handleMenuNav("exports")}
                >
                  Exports
                </button>
                {access.isAdmin && (
                  <>
                    <div className="hamburger-menu-divider" />
                    <button
                      className={`hamburger-menu-item${view === "logins" ? " active" : ""}`}
                      onClick={() => handleMenuNav("logins")}
                    >
                      Logins
                    </button>
                  </>
                )}
                <button
                  className={`hamburger-menu-item${view === "webhooks" ? " active" : ""}`}
                  onClick={() => handleMenuNav("webhooks")}
                >
                  Webhooks
                </button>
                <button
                  className={`hamburger-menu-item${view === "apikeys" ? " active" : ""}`}
                  onClick={() => handleMenuNav("apikeys")}
                >
                  API Keys
                </button>
                {access.isAdmin && (
                  <button
                    className={`hamburger-menu-item${view === "user-management" ? " active" : ""}`}
                    onClick={() => handleMenuNav("user-management")}
                  >
                    User Management
                  </button>
                )}
                <div className="hamburger-menu-divider" />
                <button
                  className={`hamburger-menu-item${view === "account" ? " active" : ""}`}
                  onClick={() => handleMenuNav("account")}
                >
                  Account
                </button>
                <div className="hamburger-menu-divider" />
                <button
                  className="hamburger-menu-item"
                  onClick={() => { handleLogout(); setMenuOpen(false); }}
                >
                  Log out
                </button>
              </div>
            )}
          </div>
        </nav>
      </header>

      <main className="app-main fade-in-up">
        <Suspense fallback={<div className="hint" style={{ padding: "1em" }}>Loading…</div>}>
        {view === "jobs" && access.isAdmin && <JobsView />}

        {view === "logins" && access.isAdmin && <Logins />}

        {view === "webhooks" && <Webhooks />}

        {view === "exports" && <Exports />}

        {view === "apikeys" && <ApiKeys me={currentUser} />}

        {view === "user-management" && access.isAdmin && <Admin meId={currentUser?.id ?? null} />}

        {view === "account" && <Account me={currentUser} />}

        {view === "dashboard" && (
          <Dashboard onSelectSource={handleSelectSource} />
        )}

        {view === "vendors" && (
          <VendorList
            onSelect={handleSelectVendor}
            selectedId={selectedVendor?.id}
            access={access}
          />
        )}

        {view === "products" && selectedVendor && (
          <ProductList
            vendor={selectedVendor}
            onSelect={handleSelectProduct}
            selectedId={selectedProduct?.id}
            access={access}
          />
        )}

        {view === "sources" && selectedProduct && (
          <SourceList
            product={selectedProduct}
            onSelectSource={handleSelectSource}
            selectedSourceId={selectedSource?.id}
            access={access}
          />
        )}

        {selectedSource &&
          (view === "browse" ||
            view === "export" ||
            view === "changelog") && (
            <>
              <nav className="source-tabs">
                {SOURCE_TABS.map((tab) => (
                  <button
                    key={tab}
                    className={view === tab ? "active" : ""}
                    onClick={() => setView(tab)}
                  >
                    {SOURCE_TAB_LABELS[tab]}
                  </button>
                ))}
              </nav>
              {view === "browse" && <DocsBrowser source={selectedSource} />}
              {view === "export" && <ExportPanel source={selectedSource} />}
              {view === "changelog" && (
                <ChangelogPanel source={selectedSource} />
              )}
            </>
          )}
        </Suspense>
      </main>
    </div>
  );
}