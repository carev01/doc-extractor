/** Shared types for DocExtractor frontend. */

export interface Vendor {
  id: string;
  name: string;
  website: string | null;
  created_at: string;
  updated_at: string;
}

export interface VendorList {
  vendors: Vendor[];
  total: number;
}

export interface Product {
  id: string;
  vendor_id: string;
  name: string;
  version: string | null;
  previous_version: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProductList {
  products: Product[];
  total: number;
}

export type SourceStatus = "pending" | "extracting" | "completed" | "failed";

export interface DocumentationSource {
  id: string;
  product_id: string;
  job_id: string | null;
  auth_realm_id?: string | null;
  name: string;
  base_url: string;
  status: SourceStatus;
  platform?: string | null;
  url_template: string | null;
  source_type: string;
  last_extracted_at: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface SourceList {
  sources: DocumentationSource[];
  total: number;
}

export interface Article {
  id: string;
  source_id: string;
  toc_entry_id: string | null;
  title: string;
  source_url: string;
  last_updated_at: string | null;
  sort_order: number;
  estimated_tokens: number;
  content_size_bytes: number;
  created_at: string;
  extracted_at: string;
}

export interface NamedRef {
  id: string;
  name: string;
}

export interface ChapterRef {
  id: string;
  title: string;
}

export interface ArticleDetail extends Article {
  content_markdown: string;
  images: ArticleImage[];
  vendor: NamedRef | null;
  product: NamedRef | null;
  parent_chapter: ChapterRef | null;
  top_level_chapter: ChapterRef | null;
}

export interface ArticleImage {
  id: string;
  original_url: string;
  local_filename: string;
  alt_text: string | null;
  file_size_bytes: number;
}

export interface ArticleList {
  articles: Article[];
  total: number;
}

export interface TOCEntry {
  id: string;
  title: string;
  url: string | null;
  level: number;
  sort_order: number;
  is_article: boolean;
  children: TOCEntry[];
  article_id: string | null;
}

export interface TOCResponse {
  source_id: string;
  entries: TOCEntry[];
}

export interface ExtractionRun {
  id: string;
  source_id: string;
  source_name?: string;
  product_name?: string;
  vendor_name?: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled" | "paused";
  control?: "cancel" | "pause" | null;
  trigger?: "manual" | "scheduled";
  kind?: string;
  /** Completed PDF run whose VLM escalation failed — eligible for retry. */
  escalation_warning?: boolean;
  /** Completed run with pages blocked by bot protection — eligible for retry. */
  blocked_warning?: boolean;
  /** Failed on the TOC-collapse data-loss guard — eligible for "Extract anyway". */
  toc_collapsed?: boolean;
  /** This run was triggered with the TOC-collapse guard overridden. */
  allow_toc_collapse?: boolean;
  blocked_count?: number;
  /** The blocked pages themselves — only populated by the single-run detail
   *  endpoint (GET /runs/{id}), not the all-runs listing. */
  blocked_pages?: { url: string; title: string | null }[];
  current_phase:
    | "toc_discovery"
    | "content_scraping"
    | "pdf_acquire"
    | "pdf_convert"
    | "pdf_split"
    | "pdf_escalate"
    | "retry_blocked"
    | null;
  firecrawl_job_id: string | null;
  articles_extracted: number;
  articles_total: number;
  articles_updated?: number;
  articles_unchanged?: number;
  articles_resumed?: number;
  attempts?: number;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  heartbeat_at?: string | null;
}

export interface RunLogs {
  run_id: string;
  log_text: string;
}

export interface ExportJobItem {
  id: string;
  source_id: string;
  source_name: string;
  product_name: string;
  vendor_name: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  format: "markdown" | "pdf";
  attempts: number;
  export_id: string | null;
  error_message: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface JobSourceRef {
  id: string;
  name: string;
  product_name: string;
  vendor_name: string;
}

export interface Job {
  id: string;
  name: string;
  enabled: boolean;
  frequency: Frequency | null;
  time_of_day: string | null;
  day_of_week: number | null;
  day_of_month: number | null;
  cron: string | null;
  timezone: string;
  next_run_at: string | null;
  last_run_at: string | null;
  source_count: number;
  sources: JobSourceRef[];
}

export interface JobList {
  jobs: Job[];
  total: number;
}

export interface JobRunItem {
  id: string;
  job_id: string;
  job_name: string | null;
  status: "pending" | "running" | "completed" | "partial" | "failed" | "cancelled";
  trigger: "manual" | "scheduled";
  sources_total: number;
  sources_done: number;
  sources_failed: number;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface ExtractionTrigger {
  run_id: string;
  source_id: string;
  status: string;
  message: string;
}

export interface ExportRequest {
  source_id: string;
  article_ids?: string[];
  toc_entry_ids?: string[];
  topic_query?: string;
  split_by?: "size" | "articles" | "tokens" | null;
  max_articles_per_file?: number;
  max_file_size_bytes?: number;
  max_tokens_per_file?: number;
  respect_chapters?: boolean;
  format?: "markdown" | "pdf";
  include_images?: boolean;
}

export interface ExportFileInfo {
  filename: string;
  article_count: number;
  size_bytes: number;
  estimated_tokens: number;
  first_article_title: string;
  last_article_title: string;
}

export interface ExportResponse {
  export_id: string;
  source_id: string;
  file_count: number;
  total_articles: number;
  total_size_bytes: number;
  zip_filename: string | null;
  files: ExportFileInfo[];
}

export interface ExportJobCreated {
  export_job_id: string;
  status: string;
}

export interface ExportListItem {
  export_id: string;
  source_id: string;
  source_name: string;
  format: "markdown" | "pdf";
  created_at: string;
  expires_at: string | null;
  file_count: number;
  files: string[];
  zip_filename: string | null;
  total_size_bytes: number;
}

export interface ExportListResponse {
  exports: ExportListItem[];
}

export interface ExportJobStatus {
  id: string;
  source_id: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  export_id: string | null;
  zip_filename: string | null;
  files: ExportFileInfo[] | null;
  error_message: string | null;
}

export interface ArticleVersion {
  id: string;
  article_id: string;
  extraction_run_id: string | null;
  content_hash: string | null;
  has_diff: boolean;
  version: string | null;
  content_size_bytes: number;
  extracted_at: string;
  source_url: string | null;
}

export interface ArticleVersionList {
  article_id: string;
  current_hash: string | null;
  versions: ArticleVersion[];
  total: number;
}

export interface VersionDiff {
  article_id: string;
  version_id: string;
  from_label: string;
  to_label: string;
  diff_text: string;
  computed: boolean;
}

export type ChangeType = "initial" | "added" | "changed" | "removed";

export interface ChangelogEntry {
  article_id: string | null;
  title: string;
  change_type: ChangeType;
  timestamp: string;
  version: string | null;
  version_id: string | null;
  extraction_run_id: string | null;
  has_diff: boolean;
}

export interface ChangelogResponse {
  source_id: string;
  entries: ChangelogEntry[];
  total: number;
}

export interface ArticleVersionDetail extends ArticleVersion {
  content_markdown: string;
}

export type ChangeStatus = "new" | "updated" | "unchanged";

export type Frequency = "hourly" | "daily" | "weekly" | "monthly";

export interface BrowseTOCEntry {
  id: string;
  title: string;
  url: string | null;
  level: number;
  sort_order: number;
  is_article: boolean;
  article_id: string | null;
  change_status: ChangeStatus | null;
  version_count: number;
  last_updated_at: string | null;
  children: BrowseTOCEntry[];
}

export interface RemovedArticle {
  article_id: string;
  title: string;
  source_url: string;
  last_extracted_at: string;
  version_count: number;
}

export interface BrowseResponse {
  source_id: string;
  latest_run_id: string | null;
  entries: BrowseTOCEntry[];
  removed: RemovedArticle[];
}

/** A platform-selector option, sourced from the backend profile registry. */
export interface ProfileOption {
  value: string;
  label: string;
}

export interface PickableSource {
  id: string;
  name: string;
  vendor_name: string;
  product_name: string;
  job_id: string | null;
  job_name: string | null;
}

// ── Consolidated dashboard overview (/dashboard/overview) ──

export interface OverviewLastRun {
  run_id: string;
  status: string;
  new: number;
  updated: number;
  unchanged: number;
}

export interface OverviewEnrichment {
  described: number;
  pending: number;
}

export interface OverviewEscalation {
  warning: boolean;
  pending_count: number;
  run_id: string | null;
}

export interface OverviewBlocked {
  warning: boolean;
  pending_count: number;
  run_id: string | null;
}

export interface OverviewSourceRow {
  id: string;
  name: string;
  vendor: string;
  product: string;
  source_type: string;
  status: string;
  last_extracted_at: string | null;
  age_seconds: number | null;
  article_count: number;
  last_run: OverviewLastRun | null;
  enrichment: OverviewEnrichment;
  escalation: OverviewEscalation;
  blocked: OverviewBlocked;
  active_run: boolean;
  job_id: string | null;
  job_name: string | null;
  next_run_at: string | null;
}

export interface OverviewAggregate {
  total: number;
  never_extracted: number;
  stale: number;
  failing: number;
  running: number;
  enrichment: { described: number; pending: number; sources_with_backlog: number };
  escalation_sources_with_warning: number;
  blocked_sources_with_warning: number;
}

export interface DashboardOverview {
  aggregate: OverviewAggregate;
  sources: OverviewSourceRow[];
}

export interface SourceEnrichment {
  source_id: string;
  vendor: string;
  product: string;
  name: string;
  described: number;
  pending: number;
  active_run: boolean;
}

export interface EnrichmentSummary {
  aggregate: { described: number; pending: number; sources_with_backlog: number };
  sources: SourceEnrichment[];
}

export interface SourceImportRow {
  row: number;
  result: string;
  vendor: string | null;
  product: string | null;
  source_name: string | null;
  message: string;
}

export interface SourceImportResult {
  created: number;
  skipped: number;
  errors: number;
  rows: SourceImportRow[];
}

export interface AuthRealm {
  id: string;
  name: string;
  login_domain: string;
  auth_type: 'form' | 'b2c' | 'oidc';
  login_url: string | null;
  status: 'active' | 'needs_login' | 'expired' | 'login_failed';
  has_username: boolean;
  has_password: boolean;
  has_totp: boolean;
  last_login_at: string | null;
  session_expires_at: string | null;
  error_message: string | null;
}

export interface AuthRealmCreate {
  name: string;
  login_domain: string;
  auth_type: 'form' | 'b2c' | 'oidc';
  login_url?: string | null;
  username?: string | null;
  password?: string | null;
  totp_secret?: string | null;
}

// ── Webhooks ──

export type WebhookEventType =
  | 'new_page'
  | 'updated_page'
  | 'removed_page'
  | 'extraction_complete';

export interface Webhook {
  id: string;
  source_id: string | null;
  url: string;
  label: string | null;
  events: WebhookEventType[];
  // The HMAC secret is never returned by the API — only whether one is set.
  has_secret: boolean;
  is_active: boolean;
  last_status_code: number | null;
  last_attempt_at: string | null;
  last_error: string | null;
  total_deliveries: number;
  total_failures: number;
  created_at: string;
  updated_at: string;
}

export interface WebhookList {
  webhooks: Webhook[];
  total: number;
}

export interface WebhookCreate {
  url: string;
  label?: string | null;
  events?: WebhookEventType[];
  secret?: string | null;
  source_id?: string | null;
  is_active?: boolean;
}

export interface WebhookUpdate {
  url?: string;
  label?: string | null;
  events?: WebhookEventType[];
  secret?: string | null;
  is_active?: boolean;
  source_id?: string | null;
}

export interface WebhookDelivery {
  id: string;
  webhook_id: string;
  event_type: string;
  run_id: string | null;
  source_id: string | null;
  status_code: number | null;
  error: string | null;
  attempt: number;
  success: boolean;
  created_at: string;
}

export interface WebhookDeliveryList {
  deliveries: WebhookDelivery[];
  total: number;
}

export interface WebhookTestResult {
  success: boolean;
  status_code: number | null;
  error: string | null;
}

// ── Auth ──

export interface AuthStatus {
  auth_enabled: boolean;
  needs_bootstrap: boolean;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface AuthUser {
  id: string;
  email: string;
  display_name: string;
  role: "admin" | "read_write" | "read_only";
  is_active: boolean;
  oauth_provider: string | null;
  created_at: string;
}

// ── User management / permissions / keys ──

export interface VendorPermission {
  vendor_id: string;
  vendor_name: string;
  level: "read_only" | "read_write";
}
export interface VendorPermissionList {
  user_id: string;
  permissions: VendorPermission[];
}

export interface ApiKeyItem {
  id: string;
  name: string;
  key_prefix: string;
  role: "admin" | "read_write" | "read_only";
  is_active: boolean;
  last_used_at: string | null;
  expires_at: string | null;
  created_at: string;
  revoked_at: string | null;
}
export interface ApiKeyCreated extends ApiKeyItem {
  raw_key: string;
}
export interface AdminApiKey extends ApiKeyItem {
  user_id: string;
  user_email: string;
}
