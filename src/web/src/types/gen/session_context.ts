// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface SessionContextGrepRequest {
  /**
   * Python 正则表达式
   */
  pattern: string;
  case_sensitive?: boolean;
  max_matches?: number;
  expected_snapshot_id?: string | null;
}
export interface SessionContextGrepResultDTO {
  session_id: string;
  pattern: string;
  case_sensitive: boolean;
  context_snapshot: SessionContextSnapshotMetadataDTO;
  total_matching_lines: number;
  returned_match_count: number;
  matches_truncated: boolean;
  matches?: SessionContextMatchDTO[];
}
export interface SessionContextSnapshotMetadataDTO {
  snapshot_id: string;
  content_sha256: string;
  generated_at: string;
  line_count: number;
  raw_message_count: number;
  byte_count: number;
  compacted: boolean;
  compaction_cutoff?: number | null;
  history_file_path?: string | null;
  expected_snapshot_id?: string | null;
  consistency: "not_checked" | "matched" | "changed";
  warning?: string | null;
}
export interface SessionContextMatchDTO {
  line_number: number;
  match_start: number;
  match_end: number;
  preview: string;
  preview_truncated_left: boolean;
  preview_truncated_right: boolean;
  line_sha256: string;
}
export interface SessionContextLineDTO {
  line_number: number;
  text: string;
  original_chars: number;
  truncated: boolean;
  line_sha256: string;
}
export interface SessionContextReadResultDTO {
  session_id: string;
  context_snapshot: SessionContextSnapshotMetadataDTO;
  line_start: number;
  line_end: number;
  has_more: boolean;
  next_line_start?: number | null;
  lines?: SessionContextLineDTO[];
}
export interface SessionRecentAssistantTextMessageDTO {
  role?: "assistant";
  type?: "text";
  text: string;
}
export interface SessionRecentTextMessagesDTO {
  session_id: string;
  rounds: number;
  user_message_count: number;
  context_snapshot: SessionContextSnapshotMetadataDTO;
  messages?: (SessionRecentUserTextMessageDTO | SessionRecentAssistantTextMessageDTO)[];
}
export interface SessionRecentUserTextMessageDTO {
  role?: "user";
  text: string;
}
