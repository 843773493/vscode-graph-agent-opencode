// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface WorkspaceContextDTO {
  workspace_id: string;
  root_path: string;
  project_type?: string | null;
  languages?: string[];
  git?: {
    [k: string]: unknown;
  };
  index_status?: {
    [k: string]: unknown;
  };
  config?: {
    [k: string]: unknown;
  };
}
export interface WorkspaceDTO {
  workspace_id: string;
  root_path: string;
  name: string;
  project_type?: string | null;
  git?: {
    [k: string]: unknown;
  };
  runtime?: {
    [k: string]: unknown;
  };
}
export interface WorkspaceFileContentDTO {
  root_path: string;
  path: string;
  name: string;
  content: string;
  language: string;
  size: number;
  modified_at?: string | null;
  revision: string;
}
export interface WorkspaceFileListDTO {
  root_path: string;
  path: string;
  items?: WorkspaceFileNodeDTO[];
  truncated?: boolean;
  limit?: number;
}
export interface WorkspaceFileNodeDTO {
  name: string;
  path: string;
  kind: "file" | "directory" | "symlink" | "other";
  has_children?: boolean;
  size?: number | null;
  modified_at?: string | null;
}
export interface WorkspaceFileUpdateRequest {
  content: string;
  expected_revision: string;
}
export interface WorkspaceIndexRebuildDTO {
  status: string;
  job_id: string;
}
export interface WorkspaceIndexStatusDTO {
  status: string;
  indexed_files?: number;
  last_updated?: string | null;
}
