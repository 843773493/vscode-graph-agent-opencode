// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface ToolTestAttemptDTO {
  attempt_id: string;
  case_id: string;
  provider_id: string;
  model: string;
  status: "queued" | "running" | "completed" | "failed";
  passed: boolean;
  tool_called: boolean;
  execution_succeeded: boolean;
  duration_ms: number;
  detail: string;
  model_calls?: number;
  reasoning_only_calls?: number;
  transient_retries?: number;
  error?: string | null;
}
export interface ToolTestProviderResultDTO {
  provider_id: string;
  model: string;
  status: "queued" | "running" | "completed" | "failed";
  completed?: number;
  total?: number;
  passed?: number;
  failed?: number;
  model_calls?: number;
  reasoning_only_calls?: number;
  transient_retries?: number;
  success_rate?: number;
}
export interface ToolTestRunDTO {
  run_id: string;
  tool_name: string;
  status: "queued" | "running" | "completed" | "failed";
  progress?: number;
  created_at: string;
  updated_at: string;
  repetitions: number;
  providers?: ToolTestProviderResultDTO[];
  attempts?: ToolTestAttemptDTO[];
  error?: string | null;
}
export interface ToolTestRunListDTO {
  items?: ToolTestRunDTO[];
}
export interface ToolTestStartRequest {
  agent_id?: string;
  provider_ids?: string[];
  repetitions?: number;
}
