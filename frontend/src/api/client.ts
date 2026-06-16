/** API client for DocExtractor backend. */

import axios from "axios";
import type {
  Vendor,
  VendorList,
  DocumentationSource,
  SourceList,
  Article,
  ArticleDetail,
  ArticleList,
  TOCResponse,
  ExtractionRun,
  ExtractionTrigger,
  ExportRequest,
  ExportResponse,
} from "../types";

const api = axios.create({
  baseURL: "http://localhost:8000/api",
  timeout: 30000,
});

// ── Vendors ──

export async function createVendor(data: {
  name: string;
  website?: string;
}): Promise<Vendor> {
  const res = await api.post("/vendors", data);
  return res.data;
}

export async function listVendors(
  skip = 0,
  limit = 50
): Promise<VendorList> {
  const res = await api.get("/vendors", { params: { skip, limit } });
  return res.data;
}

export async function getVendor(id: string): Promise<Vendor> {
  const res = await api.get(`/vendors/${id}`);
  return res.data;
}

export async function updateVendor(
  id: string,
  data: { name?: string; website?: string }
): Promise<Vendor> {
  const res = await api.patch(`/vendors/${id}`, data);
  return res.data;
}

export async function deleteVendor(id: string): Promise<void> {
  await api.delete(`/vendors/${id}`);
}

// ── Sources ──

export async function createSource(data: {
  vendor_id: string;
  name: string;
  base_url: string;
}): Promise<DocumentationSource> {
  const res = await api.post("/sources", data);
  return res.data;
}

export async function listSources(
  vendorId?: string,
  skip = 0,
  limit = 50
): Promise<SourceList> {
  const res = await api.get("/sources", {
    params: { vendor_id: vendorId, skip, limit },
  });
  return res.data;
}

export async function getSource(id: string): Promise<DocumentationSource> {
  const res = await api.get(`/sources/${id}`);
  return res.data;
}

export async function updateSource(
  id: string,
  data: { name?: string; base_url?: string }
): Promise<DocumentationSource> {
  const res = await api.patch(`/sources/${id}`, data);
  return res.data;
}

export async function deleteSource(id: string): Promise<void> {
  await api.delete(`/sources/${id}`);
}

// ── Extraction ──

export async function triggerExtraction(
  sourceId: string
): Promise<ExtractionTrigger> {
  const res = await api.post(`/extraction/trigger/${sourceId}`);
  return res.data;
}

export async function getRunStatus(runId: string): Promise<ExtractionRun> {
  const res = await api.get(`/extraction/runs/${runId}`);
  return res.data;
}

export async function listRuns(
  sourceId?: string
): Promise<{ runs: ExtractionRun[] }> {
  const res = await api.get("/extraction/runs", {
    params: { source_id: sourceId },
  });
  return res.data;
}

// ── Articles ──

export async function listArticles(
  sourceId?: string,
  search?: string,
  skip = 0,
  limit = 50
): Promise<ArticleList> {
  const res = await api.get("/articles", {
    params: { source_id: sourceId, search, skip, limit },
  });
  return res.data;
}

export async function getArticle(id: string): Promise<ArticleDetail> {
  const res = await api.get(`/articles/${id}`);
  return res.data;
}

export async function getTOC(sourceId: string): Promise<TOCResponse> {
  const res = await api.get(`/articles/toc/${sourceId}`);
  return res.data;
}

// ── Export ──

export async function exportMarkdown(
  data: ExportRequest
): Promise<ExportResponse> {
  const res = await api.post("/export/markdown", data);
  return res.data;
}

export function getDownloadUrl(exportId: string, filename: string): string {
  return `http://localhost:8000/api/export/download/${exportId}/${filename}`;
}
