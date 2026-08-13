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
  actor?: "human" | "ai" | "system";
  tool_name?: string | null;
  tool_call_id?: string | null;
  result?: "success" | "error";
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
    | "update_breakpoint"
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
  condition?: string | null;
  hit_condition?: number | null;
  log_message?: string | null;
  verified?: boolean;
  actual_line?: number | null;
  inspector_id?: string | null;
  original_line?: number | null;
  source_line?: string | null;
  previous_line?: string | null;
  next_line?: string | null;
  source_digest?: string | null;
  relocation_status?: "current" | "relocated" | "pending_update" | "source_deleted";
  relocation_message?: string | null;
  created_at: string;
}
export interface NodeDebugBreakpointRequest {
  path: string;
  line: number;
  column?: number;
  condition?: string | null;
  hit_condition?: number | null;
  log_message?: string | null;
}
export interface NodeDebugCapabilitiesDTO {
  enabled: boolean;
  default_adapter: string;
  supported_adapters?: string[];
  launch_profiles?: NodeDebugLaunchProfileDTO[];
}
export interface NodeDebugLaunchProfileDTO {
  name: string;
  adapter: string;
  runtime: string;
  supported: boolean;
  program?: string;
  working_directory?: string;
  args?: string[];
}
export interface NodeDebugConfigurationActivateRequest {
  session_id: string;
}
/**
 * 可移植方案中的断点，不包含 Inspector 安装和命中状态。
 */
export interface NodeDebugConfigurationBreakpointDTO {
  breakpoint_id: string;
  path: string;
  line: number;
  column?: number;
  condition?: string | null;
  hit_condition?: number | null;
  log_message?: string | null;
  original_line?: number | null;
  source_line?: string | null;
  previous_line?: string | null;
  next_line?: string | null;
  source_digest?: string | null;
  relocation_status?: "current" | "relocated" | "pending_update" | "source_deleted";
  relocation_message?: string | null;
  created_at: string;
}
export interface NodeDebugConfigurationCopyRequest {
  source_session_id: string;
  target_session_id: string;
  name?: string | null;
  activate?: boolean;
}
export interface NodeDebugConfigurationCreateRequest {
  session_id: string;
  name: string;
  script_path?: string | null;
  working_directory?: string;
  launch_profile_name?: string | null;
  args?: string[];
  /**
   * @maxItems 50
   */
  breakpoints?: NodeDebugBreakpointRequest[];
  activate?: boolean;
}
/**
 * 可跨会话复制的源码调试方案，不包含会话和运行时状态。
 */
export interface NodeDebugConfigurationDTO {
  schema_version?: 1;
  configuration_id: string;
  name: string;
  revision?: number;
  script_path?: string | null;
  working_directory?: string;
  launch_profile_name?: string | null;
  args?: string[];
  breakpoints?: NodeDebugConfigurationBreakpointDTO[];
  created_at: string;
  updated_at: string;
}
export interface NodeDebugConfigurationImportRequest {
  session_id: string;
  configuration: NodeDebugConfigurationDTO;
  activate?: boolean;
}
export interface NodeDebugConfigurationSummaryDTO {
  configuration_id: string;
  name: string;
  script_path?: string | null;
  launch_profile_name?: string | null;
  breakpoint_count?: number;
  revision?: number;
  updated_at: string;
}
export interface NodeDebugConfigurationUpdateRequest {
  session_id: string;
  name: string;
  script_path?: string | null;
  working_directory?: string;
  launch_profile_name?: string | null;
  args?: string[];
  /**
   * @maxItems 50
   */
  breakpoints?: NodeDebugBreakpointRequest[];
}
export interface NodeDebugEvaluationDTO {
  expression: string;
  value?: string | null;
  type?: string | null;
  description?: string | null;
  error?: string | null;
  evaluated_at: string;
}
/**
 * 会话本地状态；该文件不可作为调试方案迁移。
 */
export interface NodeDebugSessionManifestDTO {
  schema_version?: 1;
  session_id: string;
  active_configuration_id?: string | null;
  actions?: NodeDebugActionRecordDTO[];
  updated_at: string;
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
  scope?: "local" | "global";
}
export interface NodeDebugStartRequest {
  session_id: string;
  configuration_id?: string | null;
  path: string;
  working_directory?: string | null;
  launch_profile_name?: string | null;
  args?: string[];
  /**
   * @maxItems 50
   */
  breakpoints?: NodeDebugBreakpointRequest[];
}
export interface NodeDebugStateDTO {
  session_id: string;
  status: "idle" | "starting" | "running" | "paused" | "exited" | "failed";
  active_configuration_id?: string | null;
  active_configuration_name?: string | null;
  configurations?: NodeDebugConfigurationSummaryDTO[];
  script_path?: string | null;
  working_directory?: string | null;
  launch_profile_name?: string | null;
  args?: string[];
  pid?: number | null;
  paused_reason?: string | null;
  error_message?: string | null;
  call_stack?: NodeDebugStackFrameDTO[];
  last_stopped_frame?: NodeDebugStackFrameDTO | null;
  breakpoints?: NodeDebugBreakpointDTO[];
  output?: string[];
  last_evaluation?: NodeDebugEvaluationDTO | null;
  evaluations?: NodeDebugEvaluationDTO[];
  actions?: NodeDebugActionRecordDTO[];
  configuration_revision?: number;
  requires_restart?: boolean;
  source_changed_paths?: string[];
}
