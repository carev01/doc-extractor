/** API client for DocExtractor backend. */

import axios, { isAxiosError } from "axios";
import type {
  Vendor,
  VendorList,
  Product,
  ProductList,
  DocumentationSource,
  SourceList,
  ArticleDetail,
  ArticleList,
  TOCResponse,
  ExtractionRun,
  ExtractionTrigger,
  ExportRequest,
  ExportJobCreated,
  ExportJobStatus,
  ExportListResponse,
  ArticleVersionList,
  ArticleVersionDetail,
  VersionDiff,
  ChangelogResponse,
  BrowseResponse,
  Schedule,
  ScheduleConfig,
} from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");

const api = axios.create({
  baseURL: `${API_BASE}/api`,
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

// ── Products ──

export async function createProduct(data: {
  vendor_id: string;
  name: string;
}): Promise<Product> {
  const res = await api.post("/products", data);
  return res.data;
}

export async function listProducts(
  vendorId?: string,
  skip = 0,
  limit = 50
): Promise<ProductList> {
  const res = await api.get("/products", {
    params: { vendor_id: vendorId, skip, limit },
  });
  return res.data;
}

export async function updateProduct(
  id: string,
  data: { name?: string }
): Promise<Product> {
  const res = await api.patch(`/products/${id}`, data);
  return res.data;
}

export async function deleteProduct(id: string): Promise<void> {
  await api.delete(`/products/${id}`);
}

// ── Sources ──

export async function createSource(data: {
  product_id: string;
  name: string;
  base_url: string;
}): Promise<DocumentationSource> {
  const res = await api.post("/sources", data);
  return res.data;
}

export async function listSources(
  productId?: string,
  skip = 0,
  limit = 50
): Promise<SourceList> {
  const res = await api.get("/sources", {
    params: { product_id: productId, skip, limit },
  });
  return res.data;
}

export async function getSource(id: string): Promise<DocumentationSource> {
  const res = await api.get(`/sources/${id}`);
  return res.data;
}

export async function updateSource(
  id: string,
  data: { name?: string; base_url?: string; platform?: string | null; refresh_profile?: boolean }
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

export async function enqueueExport(
  data: ExportRequest
): Promise<ExportJobCreated> {
  const res = await api.post("/export", data);
  return res.data;
}

export async function getExportJob(jobId: string): Promise<ExportJobStatus> {
  const res = await api.get(`/export/jobs/${jobId}`);
  return res.data;
}

export function getDownloadUrl(exportId: string, filename: string): string {
  return `${API_BASE}/api/export/download/${exportId}/${filename}`;
}

/** URL of the self-contained zip bundle (markdown + images) for an export. */
export function getZipDownloadUrl(exportId: string): string {
  return `${API_BASE}/api/export/download/${exportId}`;
}

/** List recent (non-expired) completed exports with metadata. */
export async function listExports(): Promise<ExportListResponse> {
  const { data } = await api.get<ExportListResponse>("/export/list");
  return data;
}

/** Resolve a served /media image path to an absolute backend URL. */
export function mediaUrl(path: string): string {
  return path.startsWith("/media/") ? `${API_BASE}${path}` : path;
}

// ── Version history & changelog ──

export async function getSourceChangelog(
  sourceId: string,
  skip = 0,
  limit = 50
): Promise<ChangelogResponse> {
  const res = await api.get(`/sources/${sourceId}/changelog`, {
    params: { skip, limit },
  });
  return res.data;
}

export async function listArticleVersions(
  articleId: string,
  skip = 0,
  limit = 50
): Promise<ArticleVersionList> {
  const res = await api.get(`/articles/${articleId}/versions`, {
    params: { skip, limit },
  });
  return res.data;
}

export async function getArticleVersion(
  articleId: string,
  versionId: string
): Promise<ArticleVersionDetail> {
  const res = await api.get(`/articles/${articleId}/versions/${versionId}`);
  return res.data;
}

export async function getVersionDiff(
  articleId: string,
  versionId: string,
  against: "next" | "current" = "next"
): Promise<VersionDiff> {
  const res = await api.get(
    `/articles/${articleId}/versions/${versionId}/diff`,
    { params: { against } }
  );
  return res.data;
}

export async function browseSource(sourceId: string): Promise<BrowseResponse> {
  const res = await api.get(`/sources/${sourceId}/browse`);
  return res.data;
}

// ── Schedule ──

export async function getSchedule(sourceId: string): Promise<Schedule | null> {
  try {
    const { data } = await api.get<Schedule>(`/sources/${sourceId}/schedule`);
    return data;
  } catch (e) {
    if (isAxiosError(e) && e.response?.status === 404) return null;
    throw e;
  }
}

export async function putSchedule(
  sourceId: string, config: ScheduleConfig,
): Promise<Schedule> {
  const { data } = await api.put<Schedule>(`/sources/${sourceId}/schedule`, config);
  return data;
}

export async function deleteSchedule(sourceId: string): Promise<void> {
  await api.delete(`/sources/${sourceId}/schedule`);
}
