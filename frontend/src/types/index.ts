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

export type SourceStatus = "pending" | "extracting" | "completed" | "failed";

export interface DocumentationSource {
  id: string;
  vendor_id: string;
  name: string;
  base_url: string;
  status: SourceStatus;
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
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  trigger?: "manual" | "scheduled";
  current_phase: "toc_discovery" | "content_scraping" | null;
  firecrawl_job_id: string | null;
  articles_extracted: number;
  articles_total: number;
  articles_updated?: number;
  articles_unchanged?: number;
  error_message: string | null;
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
  zip_filename: string;
  files: ExportFileInfo[];
}

export interface ArticleVersion {
  id: string;
  article_id: string;
  extraction_run_id: string | null;
  content_hash: string | null;
  has_diff: boolean;
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

export type ChangeType = "added" | "changed" | "removed";

export interface ChangelogEntry {
  article_id: string;
  title: string;
  change_type: ChangeType;
  timestamp: string;
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

export interface ScheduleConfig {
  enabled: boolean;
  frequency: Frequency;
  time_of_day: string;        // HH:MM
  day_of_week?: number | null;
  day_of_month?: number | null;
  timezone: string;
}

export interface Schedule extends ScheduleConfig {
  source_id: string;
  cron: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_run: { id: string; status: string; completed_at: string | null } | null;
}

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
