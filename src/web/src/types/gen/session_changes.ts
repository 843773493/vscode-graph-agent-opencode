// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface SessionChangesSummaryDTO {
  files?: number;
  additions?: number;
  deletions?: number;
}
export interface SessionChangesetDTO {
  session_id: string;
  changeset_id: string;
  label: string;
  description?: string | null;
  change_kind: "all" | "turn";
  turn_id?: string | null;
  status?: "ready";
  summary?: SessionChangesSummaryDTO;
  files?: SessionFileChangeDTO[];
  generated_at: string;
}
export interface SessionFileChangeDTO {
  file_path: string;
  kind: "create" | "edit" | "delete";
  additions?: number;
  deletions?: number;
  reviewed?: boolean;
  latest_edit_id: string;
  tool_call_ids?: string[];
  execution_ids?: string[];
  turn_ids?: string[];
  before_file?: string | null;
  after_file?: string | null;
  diff_file: string;
  diff_text: string;
  before_preview?: string | null;
  after_preview?: string | null;
}
export interface SessionChangesetListDTO {
  session_id: string;
  items: SessionChangesetListItemDTO[];
}
export interface SessionChangesetListItemDTO {
  changeset_id: string;
  label: string;
  description?: string | null;
  change_kind: "all" | "turn";
  is_default?: boolean;
  turn_id?: string | null;
  summary?: SessionChangesSummaryDTO;
}
export interface SessionFileReviewRequest {
  file_path: string;
  reviewed: boolean;
}
export interface SessionFileReviewResultDTO {
  session_id: string;
  file_path: string;
  reviewed: boolean;
}
