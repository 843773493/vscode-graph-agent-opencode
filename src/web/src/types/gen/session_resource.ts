// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface SessionResourceControlRequest {
  action: "pause" | "resume" | "cancel" | "delete";
  params?: {
    [k: string]: unknown;
  };
}
export interface SessionResourceControlResultDTO {
  session_id: string;
  resource_id: string;
  kind: "job" | "background_task" | "terminal";
  action: "pause" | "resume" | "cancel" | "delete";
  status: string;
  resource?: SessionResourceDTO | null;
}
export interface SessionResourceDTO {
  resource_id: string;
  session_id: string;
  kind: "job" | "background_task" | "terminal";
  name: string;
  status: string;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  ended_at?: string | null;
  available_actions?: ("pause" | "resume" | "cancel" | "delete")[];
  metadata?: {
    [k: string]: unknown;
  };
}
export interface SessionResourceListDTO {
  session_id: string;
  items: SessionResourceDTO[];
}
