import { useState } from "react";
import type { Vendor, DocumentationSource } from "./types";
import VendorList from "./components/VendorList";
import SourceList from "./components/SourceList";
import ExportPanel from "./components/ExportPanel";
import "./App.css";

type View = "vendors" | "sources" | "export";

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
    setView("export");
  };

  return (
    <div className="app">
      <header className="app-header">
        <h1>DocExtractor</h1>
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
              <span className="sep">→</span>
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
              <span className="sep">→</span>
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

        {view === "export" && selectedSource && (
          <ExportPanel source={selectedSource} />
        )}
      </main>
    </div>
  );
}
