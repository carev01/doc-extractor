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
  PickableSource,
  DashboardResponse,
  SourceImportResult,
  AuthRealm,
  AuthRealmCreate,
  Webhook,
  WebhookList,
  WebhookCreate,
  WebhookUpdate,
  WebhookDeliveryList,
  WebhookTestResult,
  AuthStatus,
  TokenResponse,
  AuthUser,
  VendorPermissionList,
  ApiKeyItem,
  ApiKeyCreated,
  AdminApiKey,
} from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");

const api = axios.create({
  baseURL: `${API_BASE}/api`,
  timeout: 30000,
});

// ── Auth token handling ──
// The backend enforces auth only when AUTH_JWT_SECRET is set (otherwise the API
// is open). We attach the stored access token to every request and, on a 401,
// clear it and signal the app to show the login screen.
const ACCESS_KEY = "dx_access_token";
const REFRESH_KEY = "dx_refresh_token";

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_KEY);
}
export function setTokens(access: string, refresh?: string) {
  localStorage.setItem(ACCESS_KEY, access);
  if (refresh) localStorage.setItem(REFRESH_KEY, refresh);
}
export function clearTokens() {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

api.interceptors.request.use((config) => {
  const token = getAccessToken();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

api.interceptors.response.use(
  (resp) => resp,
  (error) => {
    const url: string = error.config?.url ?? "";
    // Don't treat the login/status probes as a session expiry.
    const isAuthProbe = url.includes("/auth/login") || url.includes("/auth/status");
    if (error.response?.status === 401 && !isAuthProbe) {
      clearTokens();
      window.dispatchEvent(new CustomEvent("dx-auth-expired"));
    }
    return Promise.reject(error);
  },
);

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
  auth_realm_id?: string | null;
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
  data: { name?: string; base_url?: string; platform?: string | null; refresh_profile?: boolean; url_template?: string | null; auth_realm_id?: string | null }
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

/** Retry the VLM escalation that failed on a PDF run (no Layer-A re-conversion). */
export async function retryEscalation(runId: string): Promise<ExtractionTrigger> {
  const res = await api.post(`/extraction/runs/${runId}/retry-escalation`);
  return res.data;
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

export async function listPickableSources(): Promise<PickableSource[]> {
  const res = await api.get<{ sources: PickableSource[] }>("/sources/pickable");
  return res.data.sources;
}

export async function assignSourcesToJob(
  jobId: string,
  sourceIds: string[],
): Promise<Job> {
  const res = await api.put<Job>(`/jobs/${jobId}/sources`, { source_ids: sourceIds });
  return res.data;
}

// ── Dashboard ──

export async function getDashboard(staleDays = 30): Promise<DashboardResponse> {
  const res = await api.get<DashboardResponse>("/dashboard/sources", {
    params: { stale_days: staleDays },
  });
  return res.data;
}

// ── Bulk import ──

export async function importSources(csvText: string): Promise<SourceImportResult> {
  const res = await api.post<SourceImportResult>("/sources/import", { csv: csvText });
  return res.data;
}

// ── PDF sources ──

export async function createPdfSourceFromUrl(
  productId: string,
  name: string,
  pdfUrl: string,
  authRealmId?: string | null,
): Promise<DocumentationSource> {
  const form = new FormData();
  form.append("product_id", productId);
  form.append("name", name);
  form.append("pdf_url", pdfUrl);
  if (authRealmId) form.append("auth_realm_id", authRealmId);
  const res = await api.post<DocumentationSource>("/sources/pdf", form);
  return res.data;
}

export async function uploadPdfSource(
  productId: string,
  name: string,
  file: File,
  authRealmId?: string | null,
): Promise<DocumentationSource> {
  const form = new FormData();
  form.append("product_id", productId);
  form.append("name", name);
  form.append("file", file);
  if (authRealmId) form.append("auth_realm_id", authRealmId);
  const res = await api.post<DocumentationSource>("/sources/pdf", form);
  return res.data;
}

export async function replacePdfFile(
  sourceId: string,
  file: File,
): Promise<DocumentationSource> {
  const form = new FormData();
  form.append("file", file);
  const res = await api.put<DocumentationSource>(`/sources/${sourceId}/pdf`, form);
  return res.data;
}

// ── Auth Realms ──

export const authRealmApi = {
  list: () => api.get<AuthRealm[]>('/auth-realms').then((r) => r.data),
  create: (data: AuthRealmCreate) =>
    api.post<AuthRealm>('/auth-realms', data).then((r) => r.data),
  update: (id: string, data: Partial<AuthRealmCreate>) =>
    api.patch<AuthRealm>(`/auth-realms/${id}`, data).then((r) => r.data),
  remove: (id: string) => api.delete(`/auth-realms/${id}`),
  login: (id: string) =>
    api.post<AuthRealm>(`/auth-realms/${id}/login`).then((r) => r.data),
  uploadSession: (id: string, data: { cookies: unknown[]; origins: unknown[] }) =>
    api.post<AuthRealm>(`/auth-realms/${id}/session`, data).then((r) => r.data),
  test: (id: string) =>
    api.post<AuthRealm>(`/auth-realms/${id}/test`).then((r) => r.data),
};

// ── Webhooks ──

export const webhookApi = {
  list: (sourceId?: string, isActive?: boolean) =>
    api
      .get<WebhookList>('/webhooks', {
        params: { source_id: sourceId, is_active: isActive },
      })
      .then((r) => r.data),
  create: (data: WebhookCreate) =>
    api.post<Webhook>('/webhooks', data).then((r) => r.data),
  update: (id: string, data: WebhookUpdate) =>
    api.patch<Webhook>(`/webhooks/${id}`, data).then((r) => r.data),
  remove: (id: string) => api.delete(`/webhooks/${id}`),
  test: (id: string) =>
    api.post<WebhookTestResult>(`/webhooks/${id}/test`).then((r) => r.data),
  deliveries: (id: string, limit = 50) =>
    api
      .get<WebhookDeliveryList>(`/webhooks/${id}/deliveries`, {
        params: { limit },
      })
      .then((r) => r.data),
};

// ── Auth ──

export const authApi = {
  status: () => api.get<AuthStatus>("/auth/status").then((r) => r.data),
  login: async (email: string, password: string) => {
    const r = await api.post<TokenResponse>("/auth/login", { email, password });
    setTokens(r.data.access_token, r.data.refresh_token);
    return r.data;
  },
  me: () => api.get<AuthUser>("/auth/me").then((r) => r.data),
  logout: () => clearTokens(),
};

// ── User management (admin) ──

export const usersApi = {
  list: () => api.get<{ users: AuthUser[]; total: number }>("/auth/users").then((r) => r.data),
  update: (id: string, data: { display_name?: string; role?: string; is_active?: boolean }) =>
    api.patch<AuthUser>(`/auth/users/${id}`, data).then((r) => r.data),
  remove: (id: string) => api.delete(`/auth/users/${id}`),
  getVendorPerms: (id: string) =>
    api.get<VendorPermissionList>(`/auth/users/${id}/vendor-permissions`).then((r) => r.data),
  setVendorPerms: (id: string, permissions: { vendor_id: string; level: string }[]) =>
    api.put<VendorPermissionList>(`/auth/users/${id}/vendor-permissions`, { permissions }).then((r) => r.data),
  register: (data: { email: string; display_name: string; password: string; role: string }) =>
    api.post<AuthUser>("/auth/register", data).then((r) => r.data),
};

// ── API keys (self-service + admin oversight) ──

export const keysApi = {
  listMine: () => api.get<ApiKeyItem[]>("/auth/keys").then((r) => r.data),
  create: (data: { name: string; role: string; expires_at?: string | null }) =>
    api.post<ApiKeyCreated>("/auth/keys", data).then((r) => r.data),
  rotate: (id: string) => api.post<ApiKeyCreated>(`/auth/keys/${id}/rotate`).then((r) => r.data),
  revoke: (id: string) => api.delete(`/auth/keys/${id}`),
  listAll: () => api.get<AdminApiKey[]>("/auth/admin/keys").then((r) => r.data),
};

// ── Account ──

export const accountApi = {
  changePassword: (current_password: string, new_password: string) =>
    api.post("/auth/change-password", { current_password, new_password }),
};
