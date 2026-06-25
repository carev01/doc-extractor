/** API client for DocExtractor backend. */

import axios from "axios";
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
  RunLogs,
  Frequency,
  Job,
  JobList,
  JobRunItem,
  ExtractionTrigger,
  ExportRequest,
  ExportJobCreated,
  ExportJobStatus,
  ExportJobItem,
  ExportListResponse,
  ArticleVersionList,
  ArticleVersionDetail,
  VersionDiff,
  ChangelogResponse,
  BrowseResponse,
  ProfileOption,
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

export async function bumpProductVersion(
  productId: string,
  version: string,
): Promise<{ version: string; runs: string[] }> {
  const res = await api.post(`/products/${productId}/versions/bump`, { version });
  return res.data;
}

export async function enableProductVersioning(
  productId: string,
  version: string,
): Promise<{ version: string; templatized_sources: number }> {
  const res = await api.post(`/products/${productId}/versions/enable`, { version });
  return res.data;
}

export async function detectVersionToken(
  sourceId: string,
  version: string,
): Promise<{ url_template: string | null }> {
  const res = await api.post(`/sources/${sourceId}/detect-version-token`, { version });
  return res.data;
}

// ── Sources ──

export async function createSource(data: {
  product_id: string;
  name: string;
  base_url: string;
  url_template?: string;
  platform?: string;
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
  data: { name?: string; base_url?: string; platform?: string | null; refresh_profile?: boolean; url_template?: string | null }
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
  sourceId?: string,
  status?: string,
  limit?: number
): Promise<{ runs: ExtractionRun[] }> {
  const res = await api.get("/extraction/runs", {
    params: { source_id: sourceId, status, limit },
  });
  return res.data;
}

export async function getRunLogs(runId: string): Promise<RunLogs> {
  const res = await api.get(`/extraction/runs/${runId}/logs`);
  return res.data;
}

export async function cancelRun(runId: string): Promise<void> {
  await api.post(`/extraction/runs/${runId}/cancel`);
}

export async function pauseRun(runId: string): Promise<void> {
  await api.post(`/extraction/runs/${runId}/pause`);
}

export async function resumeRun(runId: string): Promise<void> {
  await api.post(`/extraction/runs/${runId}/resume`);
}

/** Re-apply the current sanitizer to a source's already-stored articles. */
export async function resanitizeSource(
  sourceId: string
): Promise<{ source_id: string; total: number; changed: number; unchanged: number }> {
  const res = await api.post(`/extraction/resanitize/${sourceId}`, null, {
    timeout: 120000,
  });
  return res.data;
}

// ── Profiles (platform-selector options) ──

export async function getProfiles(): Promise<ProfileOption[]> {
  const res = await api.get("/profiles");
  return res.data;
}

// ── Jobs (scheduled groups of sources) ──

export async function listJobs(): Promise<JobList> {
  const res = await api.get("/jobs");
  return res.data;
}

export async function createJob(data: {
  name: string;
  enabled?: boolean;
  frequency?: Frequency | null;
  time_of_day?: string;
  day_of_week?: number | null;
  day_of_month?: number | null;
  timezone?: string;
}): Promise<Job> {
  const res = await api.post("/jobs", data);
  return res.data;
}

export async function updateJob(
  id: string,
  data: {
    name?: string;
    enabled?: boolean;
    frequency?: Frequency | null;
    time_of_day?: string;
    day_of_week?: number | null;
    day_of_month?: number | null;
    timezone?: string;
  }
): Promise<Job> {
  const res = await api.patch(`/jobs/${id}`, data);
  return res.data;
}

export async function deleteJob(id: string): Promise<void> {
  await api.delete(`/jobs/${id}`);
}

export async function assignSourceToJob(jobId: string, sourceId: string): Promise<Job> {
  const res = await api.put(`/jobs/${jobId}/sources/${sourceId}`);
  return res.data;
}

export async function unassignSourceFromJob(jobId: string, sourceId: string): Promise<Job> {
  const res = await api.delete(`/jobs/${jobId}/sources/${sourceId}`);
  return res.data;
}

export async function runJob(id: string): Promise<JobRunItem> {
  const res = await api.post(`/jobs/${id}/run`);
  return res.data;
}

export async function listJobRuns(id: string, limit = 20): Promise<JobRunItem[]> {
  const res = await api.get(`/jobs/${id}/runs`, { params: { limit } });
  return res.data;
}

/** Recent JobRuns across all jobs (for the Jobs Activity feed). */
export async function listAllJobRuns(limit = 30): Promise<JobRunItem[]> {
  const res = await api.get("/jobs/runs", { params: { limit } });
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

/** Delete a generated export (its files + record) now, before it expires. */
export async function deleteExport(exportId: string): Promise<void> {
  await api.delete(`/export/${exportId}`);
}

/** List export jobs (the export queue) with names, for the Jobs view. */
export async function listExportJobs(
  status?: string,
  limit?: number
): Promise<{ jobs: ExportJobItem[] }> {
  const { data } = await api.get("/export/jobs", { params: { status, limit } });
  return data;
}

/** Cancel a queued export job. */
export async function cancelExportJob(jobId: string): Promise<void> {
  await api.post(`/export/jobs/${jobId}/cancel`);
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
