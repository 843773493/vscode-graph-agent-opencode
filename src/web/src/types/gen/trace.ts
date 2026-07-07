// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface TraceEventDTO {
  event_id: string;
  session_id: string;
  job_id?: string | null;
  type:
    | "agent_start"
    | "llm_request"
    | "tool_call_start"
    | "tool_call_end"
    | "agent_end"
    | "error"
    | "job_created"
    | "job_started"
    | "job_completed"
    | "job_cancelled"
    | "job_failed"
    | "status_change"
    | "agent_step"
    | "text_start"
    | "text_delta"
    | "text_end"
    | "message_created"
    | "session_interrupted";
  phase: "agent" | "llm" | "tool" | "error" | "job" | "text" | "system" | "status" | "message" | "session";
  title: string;
  content: string;
  status?: string | null;
  tool_name?: string | null;
  skill_names?: string[];
  step_id?: string | null;
  timestamp: string;
  raw?: {
    [k: string]: unknown;
  };
}
