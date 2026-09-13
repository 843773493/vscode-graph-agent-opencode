// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface AgentDebugStateDTO {
  job_id: string;
  session_id: string;
  enabled: boolean;
  mode: "ai" | "human" | "collaborative";
  paused: boolean;
  active_stop?: DebugStopSnapshotDTO | null;
  last_stop?: DebugStopSnapshotDTO | null;
  breakpoints?: DebugBreakpointDTO[];
  actions?: DebugActionRecordDTO[];
  stop_count?: number;
}
export interface DebugStopSnapshotDTO {
  stop_id: string;
  job_id: string;
  session_id: string;
  point: "tool_before" | "tool_after" | "llm_before";
  reason: string;
  tool_name?: string | null;
  args?: {
    [k: string]: unknown;
  };
  result?: string | null;
  breakpoint_id?: string | null;
  mode: "ai" | "human" | "collaborative";
  explanation: string;
  stopped_at: string;
  sequence: number;
}
export interface DebugBreakpointDTO {
  breakpoint_id: string;
  kind: "tool_before" | "tool_after" | "llm_before";
  tool_name?: string | null;
  enabled?: boolean;
  created_at: string;
}
export interface DebugActionRecordDTO {
  action_id: string;
  job_id: string;
  session_id: string;
  action: string;
  actor: "human" | "ai" | "system";
  mode: "ai" | "human" | "collaborative";
  message: string;
  created_at: string;
}
