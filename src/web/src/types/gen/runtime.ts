// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface RuntimeInfoDTO {
  pid: number;
  uptime_seconds: number;
  workspace_id: string;
  active_jobs: number;
  loaded_agents: string[];
  storage: RuntimeStorageDTO;
}
export interface RuntimeStorageDTO {
  root: string;
  artifact_dir: string;
  log_dir: string;
  cache_dir: string;
}
export interface RuntimeShutdownDTO {
  status: string;
  delay_seconds: number;
}
export interface RuntimeShutdownResultDTO {
  status: string;
  delay_seconds: number;
}
export interface RuntimeStatusDTO {
  pid: number;
  uptime_seconds: number;
  workspace_id: string;
  active_jobs: number;
  loaded_agents?: string[];
  storage: RuntimeStorageDTO;
}
export interface UiSnapshotResultDTO {
  html_path: string;
  status?: string;
}
