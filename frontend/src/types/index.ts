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
  name: string;
  base_url: string;
  status: SourceStatus;
  platform?: string | null;
  url_template: string | null;
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
  current_phase: "toc_discovery" | "content_scraping" | null;
  firecrawl_job_id: string | null;
  articles_extracted: number;
  articles_total: number;
  articles_updated?: number;
  articles_unchanged?: number;
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
