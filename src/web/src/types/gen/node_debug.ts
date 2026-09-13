// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface NodeDebugActionRecordDTO {
  action_id: string;
  session_id: string;
  action: string;
  message: string;
  created_at: string;
}
export interface NodeDebugActionRequest {
  session_id: string;
  action:
    | "continue"
    | "pause"
    | "step_over"
    | "step_into"
    | "step_out"
    | "set_breakpoint"
    | "clear_breakpoint"
    | "evaluate"
    | "stop";
  params?: {
    [k: string]: unknown;
  };
}
export interface NodeDebugBreakpointDTO {
  breakpoint_id: string;
  path: string;
  line: number;
  column?: number;
  verified?: boolean;
  actual_line?: number | null;
  inspector_id?: string | null;
  created_at: string;
}
export interface NodeDebugBreakpointRequest {
  path: string;
  line: number;
  column?: number;
}
export interface NodeDebugEvaluationDTO {
  expression: string;
  value?: string | null;
  type?: string | null;
  description?: string | null;
  error?: string | null;
  evaluated_at: string;
}
export interface NodeDebugStackFrameDTO {
  call_frame_id: string;
  function_name: string;
  url: string;
  path?: string | null;
  line: number;
  column: number;
  scope_names?: string[];
  variables?: NodeDebugVariableDTO[];
}
export interface NodeDebugVariableDTO {
  name: string;
  value: string;
  type?: string | null;
  object_id?: string | null;
}
export interface NodeDebugStartRequest {
  session_id: string;
  path: string;
  args?: string[];
  /**
   * @maxItems 50
   */
  breakpoints?: NodeDebugBreakpointRequest[];
}
export interface NodeDebugStateDTO {
  session_id: string;
  status: "idle" | "starting" | "running" | "paused" | "exited" | "failed";
  script_path?: string | null;
  args?: string[];
  pid?: number | null;
  inspector_url?: string | null;
  paused_reason?: string | null;
  error_message?: string | null;
  call_stack?: NodeDebugStackFrameDTO[];
  breakpoints?: NodeDebugBreakpointDTO[];
  output?: string[];
  last_evaluation?: NodeDebugEvaluationDTO | null;
  actions?: NodeDebugActionRecordDTO[];
}
