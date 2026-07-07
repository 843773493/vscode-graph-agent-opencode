// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface ConfigDTO {
  default_model: string;
  default_orchestration: string;
  max_concurrent_agents?: number;
  allow_shell_tools?: boolean;
  ignored_paths?: string[];
  auto_summarize?: boolean;
  metadata?: {
    [k: string]: unknown;
  };
}
export interface ConfigUpdateRequest {
  default_model?: string | null;
  default_orchestration?: string | null;
  max_concurrent_agents?: number | null;
  allow_shell_tools?: boolean | null;
  ignored_paths?: string[] | null;
  auto_summarize?: boolean | null;
}
