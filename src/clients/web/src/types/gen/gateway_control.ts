// 该文件由程序生成，请勿手写。
/* tslint:disable */
/* eslint-disable */
/**
/* This file was automatically generated from pydantic models by running pydantic2ts.
/* Do not modify it by hand - just update the pydantic models and then re-run the script
*/

export interface GatewayResourceDTO {
  gateway_connection_id?: string | null;
  gateway_name: string;
  workspace_id: string;
  workspace_name: string;
  connection_kind: "local" | "remote_gateway";
  session_id: string;
  session_title: string;
  resource: SessionResourceDTO;
}
/**
 * 会话后台连接。
 *
 * 这里只描述可保留、可重新打开或可连接的长生命周期对象，例如持久终端、
 * 浏览器页面和持续后台任务。一次性 agent job 属于执行状态/事件流，不进入该列表。
 */
export interface SessionResourceDTO {
  resource_id: string;
  session_id: string;
  kind: "background_task" | "terminal" | "browser";
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
export interface GatewayResourceListDTO {
  items?: GatewayResourceDTO[];
  errors?: GatewayResourceScopeErrorDTO[];
}
export interface GatewayResourceScopeErrorDTO {
  scope_key: string;
  label: string;
  message: string;
}
export interface GatewaySessionSearchMatchDTO {
  workspace_id: string;
  workspace_name: string;
  node_id: string;
  node_kind: "workspace_folder" | "workspace" | "folder" | "session";
  name: string;
  session_id?: string | null;
  relative_path: string;
  storage_relative_path?: string | null;
  breadcrumb_names?: string[];
  breadcrumb_node_ids?: string[];
}
export interface GatewaySessionSearchResultsDTO {
  items?: GatewaySessionSearchMatchDTO[];
  workspaces?: GatewaySessionSearchWorkspaceStatusDTO[];
  total?: number;
}
export interface GatewaySessionSearchWorkspaceStatusDTO {
  workspace_id: string;
  workspace_name: string;
  status: "available" | "stale" | "unavailable";
  error?: string | null;
}
export interface GenerationOutputDTO {
  kind?: "session";
  workspace_id: string;
  session_id: string;
  title?: string | null;
  navigation_path?: string[];
  storage_relative_path?: string | null;
}
export interface GenerationRunDTO {
  run_id: string;
  generator_id: string;
  idempotency_key: string;
  status:
    | "planned"
    | "dispatching"
    | "running"
    | "reporting"
    | "completed"
    | "partial"
    | "failed"
    | "cancelled"
    | "skipped";
  trigger_type: string;
  scheduled_for: string;
  outputs?: GenerationOutputDTO[];
  execution_workspace_id?: string | null;
  message_id?: string | null;
  job_id?: string | null;
  report_back_job_id?: string | null;
  error?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
}
export interface GenerationRunListDTO {
  items?: GenerationRunDTO[];
}
export interface GeneratorContextSourceDTO {
  kind?: "fresh" | "live_session" | "snapshot";
  workspace_id?: string | null;
  session_id?: string | null;
  snapshot_id?: string | null;
}
export interface GeneratorDefinitionCreateRequest {
  name: string;
  generator_type?: GeneratorTypeRefDTO;
  enabled?: boolean;
  trigger?: GeneratorTriggerDTO;
  placement: GeneratorPlacementDTO;
  execution_workspace_id: string;
  context_source?: GeneratorContextSourceDTO;
  created_from?: SessionLocatorDTO | null;
  naming?: GeneratorNamingDTO;
  session_strategy?: GeneratorSessionStrategyDTO;
  policies?: GeneratorPoliciesDTO;
  ui_policy?: GeneratorUIPolicyDTO;
  config?: {
    [k: string]: unknown;
  };
}
export interface GeneratorTypeRefDTO {
  type_id: string;
  version: string;
}
export interface GeneratorTriggerDTO {
  type?: "manual" | "cron" | "interval";
  expression?: string | null;
  interval_seconds?: number | null;
  timezone?: string;
}
export interface GeneratorPlacementDTO {
  kind: "workspace" | "session" | "session_folder";
  workspace_id: string;
  session_id?: string | null;
  folder_id?: string | null;
}
export interface SessionLocatorDTO {
  workspace_id: string;
  session_id: string;
}
export interface GeneratorNamingDTO {
  title_template?: string;
  /**
   * @maxItems 20
   */
  path_template?:
    | []
    | [string]
    | [string, string]
    | [string, string, string]
    | [string, string, string, string]
    | [string, string, string, string, string]
    | [string, string, string, string, string, string]
    | [string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [string, string, string, string, string, string, string, string, string, string, string, string, string, string]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ]
    | [
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string,
        string
      ];
}
export interface GeneratorSessionStrategyDTO {
  mode?: "new_per_run" | "continue_existing" | "fork_new_and_report_back";
  target?: SessionLocatorDTO | null;
  concurrency?: "queue";
  report_back?: "none" | "link" | "summary" | "summary_and_link" | "full" | "continue_agent";
}
export interface GeneratorPoliciesDTO {
  overlap?: "allow";
  misfire?: "skip" | "run_latest" | "catch_up";
  mount_missing?: "pause" | "fail";
  delete_outputs?: "keep" | "cascade";
}
export interface GeneratorUIPolicyDTO {
  on_run_started?: "stay" | "open_generated";
  on_run_completed?: "none" | "notify" | "open_generated" | "open_on_failure";
}
export interface GeneratorDefinitionDTO {
  name: string;
  generator_type?: GeneratorTypeRefDTO;
  enabled?: boolean;
  trigger?: GeneratorTriggerDTO;
  placement: GeneratorPlacementDTO;
  execution_workspace_id: string;
  context_source?: GeneratorContextSourceDTO;
  created_from?: SessionLocatorDTO | null;
  naming?: GeneratorNamingDTO;
  session_strategy?: GeneratorSessionStrategyDTO;
  policies?: GeneratorPoliciesDTO;
  ui_policy?: GeneratorUIPolicyDTO;
  config?: {
    [k: string]: unknown;
  };
  generator_id: string;
  status?: "ready" | "paused" | "blocked";
  status_reason?: string | null;
  revision?: number;
  created_at: string;
  updated_at: string;
}
export interface GeneratorDefinitionListDTO {
  revision: string;
  items?: GeneratorDefinitionDTO[];
}
export interface GeneratorDefinitionUpdateRequest {
  name?: string | null;
  enabled?: boolean | null;
  trigger?: GeneratorTriggerDTO | null;
  placement?: GeneratorPlacementDTO | null;
  execution_workspace_id?: string | null;
  context_source?: GeneratorContextSourceDTO | null;
  naming?: GeneratorNamingDTO | null;
  session_strategy?: GeneratorSessionStrategyDTO | null;
  policies?: GeneratorPoliciesDTO | null;
  ui_policy?: GeneratorUIPolicyDTO | null;
  config?: {
    [k: string]: unknown;
  } | null;
}
export interface GeneratorManualRunRequest {
  idempotency_key?: string | null;
}
export interface GeneratorPlacementPreviewDTO {
  preview_kind?: "logical_physical_path_template";
  title: string;
  path_segments: string[];
  session_path_segment: string;
  relative_path: string;
}
export interface GeneratorPlacementPreviewRequest {
  name: string;
  naming: GeneratorNamingDTO;
  session_title?: string;
  generated_at?: string | null;
  placement?: GeneratorPlacementDTO | null;
  session_strategy?: GeneratorSessionStrategyDTO;
}
export interface WorkspaceFolderCreateRequest {
  name: string;
  parent_node_id?: string | null;
  position?: number | null;
}
export interface WorkspaceNavigationBreadcrumbDTO {
  revision: string;
  items?: WorkspaceNavigationNodeDTO[];
}
export interface WorkspaceNavigationNodeDTO {
  node_id: string;
  kind: "workspace_folder" | "workspace_ref";
  name: string;
  parent_node_id?: string | null;
  workspace_id?: string | null;
  position?: number;
}
export interface WorkspaceNavigationNodeUpdateRequest {
  name?: string | null;
  parent_node_id?: string | null;
  position?: number | null;
}
export interface WorkspaceNavigationPlacementRequest {
  node_id: string;
  parent_node_id?: string | null;
  mode: "before" | "after" | "last";
  target_node_id?: string | null;
}
export interface WorkspaceNavigationReorderRequest {
  parent_node_id?: string | null;
  /**
   * @minItems 1
   */
  node_ids: [string, ...string[]];
}
export interface WorkspaceNavigationTreeDTO {
  revision: string;
  nodes?: WorkspaceNavigationNodeDTO[];
}
