import { useState } from "react";
import type { Vendor, DocumentationSource } from "./types";
import VendorList from "./components/VendorList";
import SourceList from "./components/SourceList";
import ExportPanel from "./components/ExportPanel";
import ChangelogPanel from "./components/ChangelogPanel";
import DocsBrowser from "./components/DocsBrowser";
import ScheduleControl from "./components/ScheduleControl";
import "./App.css";

type View = "vendors" | "sources" | "browse" | "export" | "changelog" | "schedule";
const SOURCE_TABS = ["browse", "export", "changelog", "schedule"] as const;
const SOURCE_TAB_LABELS: Record<string, string> = {
  browse: "Browse",
  export: "Export",
  changelog: "Changelog",
  schedule: "Schedule",
};

export default function App() {
  const [view, setView] = useState<View>("vendors");
  const [selectedVendor, setSelectedVendor] = useState<Vendor | null>(null);
  const [selectedSource, setSelectedSource] =
    useState<DocumentationSource | null>(null);

  const handleSelectVendor = (vendor: Vendor) => {
    setSelectedVendor(vendor);
    setSelectedSource(null);
    setView("sources");
  };

  const handleSelectSource = (source: DocumentationSource) => {
    setSelectedSource(source);
    setView("browse");
  };

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
              setSelectedSource(null);
            }}
          >
            Vendors
          </button>
          {selectedVendor && (
            <>
              <span className="sep">/</span>
              <button
                className={view === "sources" ? "active" : ""}
                onClick={() => {
                  setView("sources");
                  setSelectedSource(null);
                }}
              >
                {selectedVendor.name}
              </button>
            </>
          )}
          {selectedSource && (
            <>
              <span className="sep">/</span>
              <button className="active">{selectedSource.name}</button>
            </>
          )}
        </nav>
      </header>

      <main className="app-main">
        {view === "vendors" && (
          <VendorList
            onSelect={handleSelectVendor}
            selectedId={selectedVendor?.id}
          />
        )}

        {view === "sources" && selectedVendor && (
          <SourceList
            vendor={selectedVendor}
            onSelectSource={handleSelectSource}
            selectedSourceId={selectedSource?.id}
          />
        )}

        {selectedSource &&
          (view === "browse" ||
            view === "export" ||
            view === "changelog" ||
            view === "schedule") && (
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
              {view === "schedule" && (
                <ScheduleControl source={selectedSource} />
              )}
            </>
          )}
      </main>
    </div>
  );
}
