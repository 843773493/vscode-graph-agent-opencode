// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface SessionContextItemDTO {
  kind: string;
  locator: string;
  role?: string | null;
  record_index?: number | null;
  text?: string | null;
  reasoning?: string | null;
  tool_summary?: string[];
  tool_calls?: {
    [k: string]: unknown;
  }[];
  tool_results?: {
    [k: string]: unknown;
  }[];
  data?: {
    [k: string]: unknown;
  } | null;
  raw_record?: {
    [k: string]: unknown;
  } | null;
  truncated?: boolean;
}
export interface SessionContextPartialErrorDTO {
  resource: string;
  error: string;
}
export interface SessionContextReadRequest {
  resource: string;
  view?: "overview" | "messages" | "records" | "information" | "inventory";
  include?: ("visible_text" | "reasoning" | "tool_summary" | "tool_calls" | "tool_results" | "system" | "raw_record")[];
  recent_rounds?: number;
  include_initial_goal?: boolean;
  cursor?: string | null;
  limit?: number;
  max_chars?: number;
  expected_revision?: string | null;
}
export interface SessionContextReadResultDTO {
  resource: string;
  view: "overview" | "messages" | "records" | "information" | "inventory";
  revision: string;
  compacted?: boolean;
  compaction_cutoff?: number | null;
  raw_message_count?: number;
  effective_record_count?: number;
  returned_chars?: number;
  truncated?: boolean;
  has_more?: boolean;
  next_cursor?: string | null;
  items?: SessionContextItemDTO[];
  partial_errors?: SessionContextPartialErrorDTO[];
  omitted_partial_error_count?: number;
}
export interface SessionContextSearchMatchDTO {
  locator: string;
  preview: string;
  source: "effective_context" | "session_catalog" | "session_information";
  revision: string;
  record_index?: number | null;
  match_start: number;
  match_end: number;
}
export interface SessionContextSearchRequest {
  resource: string;
  query: string;
  sources?: ("effective_context" | "session_catalog" | "session_information")[];
  match_mode?: "literal" | "regex";
  case_sensitive?: boolean;
  max_results?: number;
  max_chars?: number;
  cursor?: string | null;
  expected_revision?: string | null;
}
export interface SessionContextSearchResultDTO {
  resource: string;
  query: string;
  match_mode: "literal" | "regex";
  revision: string;
  returned_chars?: number;
  truncated?: boolean;
  has_more?: boolean;
  next_cursor?: string | null;
  total_matches?: number;
  matches?: SessionContextSearchMatchDTO[];
  partial_errors?: SessionContextPartialErrorDTO[];
  omitted_partial_error_count?: number;
}
