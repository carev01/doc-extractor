import { useEffect, useRef, useState } from "react";
import type { Vendor, Product, DocumentationSource, AuthUser } from "./types";
import { authApi, getAccessToken } from "./api/client";
import { Login } from "./views/Login";
import { ApiKeys } from "./views/ApiKeys";
import { Admin } from "./views/Admin";
import { Account } from "./views/Account";
import VendorList from "./components/VendorList";
import ProductList from "./components/ProductList";
import SourceList from "./components/SourceList";
import JobsView from "./components/JobsView";
import Dashboard from "./components/Dashboard";
import ExportPanel from "./components/ExportPanel";
import ChangelogPanel from "./components/ChangelogPanel";
import DocsBrowser from "./components/DocsBrowser";
import { Logins } from "./views/Logins";
import { Webhooks } from "./views/Webhooks";
import "./App.css";

type View =
  | "vendors"
  | "products"
  | "sources"
  | "browse"
  | "export"
  | "changelog"
  | "jobs"
  | "dashboard"
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

export default function App() {
  const [view, setView] = useState<View>("vendors");
  const [selectedVendor, setSelectedVendor] = useState<Vendor | null>(null);
  const [selectedProduct, setSelectedProduct] = useState<Product | null>(null);
  const [selectedSource, setSelectedSource] =
    useState<DocumentationSource | null>(null);

  // Auth gate. 'loading' = probing; 'open' = auth disabled on the server;
  // 'login' = sign-in required; 'authed' = signed in.
  const [authGate, setAuthGate] = useState<"loading" | "open" | "login" | "authed">("loading");
  const [needsBootstrap, setNeedsBootstrap] = useState(false);
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);

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
          <button
            className={view === "jobs" ? "active" : ""}
            onClick={() => setView("jobs")}
          >
            Jobs
          </button>
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
                  className={`hamburger-menu-item${view === "logins" ? " active" : ""}`}
                  onClick={() => handleMenuNav("logins")}
                >
                  Logins
                </button>
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
                {currentUser?.role === "admin" && (
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
        {view === "jobs" && <JobsView />}

        {view === "logins" && <Logins />}

        {view === "webhooks" && <Webhooks />}

        {view === "apikeys" && <ApiKeys me={currentUser} />}

        {view === "user-management" && <Admin meId={currentUser?.id ?? null} />}

        {view === "account" && <Account me={currentUser} />}

        {view === "dashboard" && (
          <Dashboard onSelectSource={handleSelectSource} />
        )}

        {view === "vendors" && (
          <VendorList
            onSelect={handleSelectVendor}
            selectedId={selectedVendor?.id}
          />
        )}

        {view === "products" && selectedVendor && (
          <ProductList
            vendor={selectedVendor}
            onSelect={handleSelectProduct}
            selectedId={selectedProduct?.id}
          />
        )}

        {view === "sources" && selectedProduct && (
          <SourceList
            product={selectedProduct}
            onSelectSource={handleSelectSource}
            selectedSourceId={selectedSource?.id}
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
      </main>
    </div>
  );
}