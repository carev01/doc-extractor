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
  extracted_at: string;
}

export interface ArticleDetail extends Article {
  content_markdown: string;
  images: ArticleImage[];
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
  status: "running" | "completed" | "failed";
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
  files: ExportFileInfo[];
}
