// 该文件是前端业务类型适配层，封装后端实际返回结构。
// 本目录业务代码统一从这里导入类型；后端 DTO 直接别名到生成文件，避免手写协议漂移。

export type { AgentDTO as Agent } from "./gen/agent";
export type { JobDTO as Job, JobStatus } from "./gen/job";
import type { LLMRequestLogRecordDTO } from "./gen/llm_request_log";
import type { PendingRequestOrderItem as PendingRequestOrderItemType } from "./gen/pending_request";
export type { AttachmentRef } from "./gen/attachment";
export type {
  AgentStateMessagesDTO as AgentStateMessages,
  MessageDTO as Message,
  MessageReplayAccepted,
  MessageReplayRequest,
  MessageRunAccepted,
  MessageRunRequest,
  RunOptions,
} from "./gen/message";
export type {
  PendingRequestDTO as PendingRequest,
  PendingRequestListDTO as PendingRequestList,
  PendingRequestOrderItem,
  PendingRequestReorderRequest,
  PendingRequestUpdateRequest,
} from "./gen/pending_request";
export type PendingRequestKind = PendingRequestOrderItemType["kind"];

export type SessionGoalStatus =
  | "active"
  | "paused"
  | "blocked"
  | "usage_limited"
  | "budget_limited"
  | "complete";

export interface SessionGoal {
  goal_id: string;
  session_id: string;
  objective: string;
  status: SessionGoalStatus;
  token_budget: number | null;
  tokens_used: number;
  time_used_seconds: number;
  created_at: string;
  updated_at: string;
}

export interface SessionGoalUpdateRequest {
  objective?: string;
  status?: SessionGoalStatus;
  token_budget?: number | null;
  replace?: boolean;
}
export type {
  DeleteSessionResultDTO as DeleteSessionResult,
  SessionCompactResultDTO as SessionCompactResult,
  SessionInformationSnapshotDTO as SessionInformationSnapshot,
  SessionDTO as Session,
  SessionInterruptResultDTO as InterruptSessionResult,
  SessionUpdateRequest,
} from "./gen/session";
import type {
  SessionResourceControlResultDTO,
  SessionResourceDTO,
  SessionResourceListDTO,
} from "./gen/session_resource";
import type {
  SessionChangesSummaryDTO,
  SessionChangesetDTO,
  SessionChangesetListDTO,
  SessionChangesetListItemDTO,
  SessionFileChangeDTO,
  SessionFileReviewResultDTO,
} from "./gen/session_changes";
import type { TraceEventDTO } from "./gen/trace";
export type { TraceEventDTO as SessionStreamEvent } from "./gen/trace";
export type {
  SessionTurnBootstrapDTO as SessionTurnBootstrap,
  StaleTurnCursorErrorDTO as StaleTurnCursorError,
  TurnAttachmentDTO as TurnAttachment,
  TurnDetailBatchDTO as TurnDetailBatch,
  TurnDetailBatchRequest,
  TurnDetailDTO as TurnDetail,
  TurnJobSummaryDTO as TurnJobSummary,
  TurnPageDTO as TurnPage,
  TurnSummaryDTO as TurnSummary,
  TurnUserMessageDTO as TurnUserMessage,
  TurnUserMessageSummaryDTO as TurnUserMessageSummary,
} from "./gen/turn";
export type {
  FileTreeShortcutDTO as FileTreeShortcut,
  FileTreeShortcutRequest,
  SessionFileTreeSettingsDTO as SessionFileTreeSettings,
  WorkspaceDTO as WorkspaceInfo,
  WorkspaceFileContentDTO as WorkspaceFileContent,
  WorkspaceFileChangeBatchDTO as WorkspaceFileStreamBatch,
  WorkspaceFileChangeDTO as WorkspaceFileStreamChange,
  WorkspaceFileCreateRequest,
  WorkspaceFileListDTO as WorkspaceFileList,
  WorkspaceFileNodeDTO as WorkspaceFileNode,
  WorkspaceFilePasteRequest,
  WorkspaceFileRevealDTO as WorkspaceFileReveal,
  WorkspaceFileUpdateRequest,
} from "./gen/workspace";

export type LLMRequestLogRecord = Omit<
  LLMRequestLogRecordDTO,
  "request" | "response" | "upstream"
> & {
  request: Record<string, unknown>;
  response: Record<string, unknown>;
  upstream: Record<string, unknown>;
};

export type SessionResource = Omit<
  SessionResourceDTO,
  "available_actions" | "metadata"
> & {
  available_actions: NonNullable<SessionResourceDTO["available_actions"]>;
  metadata: Record<string, unknown>;
};

export type SessionResourceList = Omit<SessionResourceListDTO, "items"> & {
  items: SessionResource[];
};

export type SessionResourceControlResult = Omit<
  SessionResourceControlResultDTO,
  "resource"
> & {
  resource?: SessionResource | null;
};

type TraceRaw = NonNullable<TraceEventDTO["raw"]> & {
  part_id?: string | null;
  payload?: Record<string, unknown>;
  session_id?: string;
  agent_id?: string | null;
  step_id?: string | null;
};

export interface TraceEvent
  extends Omit<TraceEventDTO, "session_id" | "phase" | "title" | "content" | "raw"> {
  session_id: string;
  phase?: TraceEventDTO["phase"];
  title?: string;
  content?: string;
  agent_id?: string | null;
  payload?: Record<string, unknown>;
  raw?: TraceRaw;
}

export type SessionChangesSummary = Required<SessionChangesSummaryDTO>;

export type SessionChangesetKind = "all" | "turn";
export type SessionFileChangeKind = "create" | "edit" | "delete";

export type SessionChangesetListItem = Omit<
  SessionChangesetListItemDTO,
  "is_default" | "summary"
> & {
  is_default: boolean;
  summary: SessionChangesSummary;
};

export type SessionChangesetList = Omit<SessionChangesetListDTO, "items"> & {
  items: SessionChangesetListItem[];
};

export type SessionFileChange = Omit<
  SessionFileChangeDTO,
  "additions" | "deletions" | "reviewed" | "tool_call_ids" | "turn_ids"
> & {
  kind: SessionFileChangeKind;
  additions: number;
  deletions: number;
  reviewed: boolean;
  tool_call_ids: string[];
  turn_ids: string[];
};

export type SessionChangeset = Omit<
  SessionChangesetDTO,
  "status" | "summary" | "files"
> & {
  change_kind: SessionChangesetKind;
  status: "ready";
  summary: SessionChangesSummary;
  files: SessionFileChange[];
};

export type SessionFileReviewResult = SessionFileReviewResultDTO;

export interface APIResponse<T> {
  code: number;
  message: string;
  data: T | null;
  request_id: string;
}

export interface CursorPage<T> {
  items: T[];
  next_cursor?: string | null;
  has_more?: boolean;
}

export interface GatewayWorkspace {
  workspace_id: string;
  parent_workspace_id?: string | null;
  name: string;
  root_path: string;
  backend_url: string;
  connection_kind: "local" | "remote_gateway";
  status: "ready" | "offline";
  active: boolean;
  managed: boolean;
  removable: boolean;
  system_default: boolean;
  runtime_action?:
    | "start_managed_backend"
    | "safe_restart_managed_backend"
    | "force_restart_managed_backend"
    | "reconnect_remote_gateway"
    | "probe_external_backend";
  config_reload?: GatewayConfigReloadStatus;
  remote: GatewayRemoteConnectionSummary | null;
  services: Record<string, GatewayServiceStatus>;
  connection_error?: string | null;
  checked_at: string;
}

export interface GatewayConfigReloadStatus {
  available: boolean;
  healthy?: boolean | null;
  revision?: string | null;
  restart_required: boolean;
  reason?: "invalid_config" | "restart_required" | "apply_failed" | null;
  changed_sections: string[];
  last_error?: string | null;
  error?: string | null;
}

export interface GatewayRemoteConnectionSummary {
  gateway_connection_id: string;
  remote_workspace_id: string;
  gateway_id: string;
  name: string;
  host: string;
  port: number;
  username: string;
  ssh_config_host?: string | null;
  remote_gateway_port: number;
}

export interface GatewayServiceStatus {
  status: "ready" | "offline" | "unavailable";
  health_path: string;
  local_url?: string | null;
  local_port?: number | null;
  remote_host?: string | null;
  remote_port?: number | null;
  error?: string | null;
}

export interface GatewayWorkspaceList {
  active_workspace_id: string | null;
  items: GatewayWorkspace[];
}

export type GatewayPortForwardProtocol = "http" | "https" | "tcp";

export type GatewayPortForwardStatus =
  | "starting"
  | "active"
  | "error"
  | "stopped";

export interface GatewayPortForward {
  forward_id: string;
  workspace_id: string;
  connection_id: string;
  remote_host: string;
  remote_port: number;
  local_host: string;
  local_port: number;
  protocol: GatewayPortForwardProtocol;
  label: string | null;
  status: GatewayPortForwardStatus;
  error: string | null;
  local_url: string | null;
}

export interface GatewayPortForwardList {
  items: GatewayPortForward[];
}

export interface CreateGatewayPortForwardRequest {
  remote_port: number;
  local_port?: number | null;
  protocol: GatewayPortForwardProtocol;
  label?: string | null;
}

export interface GatewayManagedWorkspace {
  workspace_id: string;
  name: string;
  root_path: string;
  status: "ready" | "offline";
  removable: boolean;
  system_default: boolean;
}

export interface GatewayManagedWorkspaceList {
  gateway_connection_id?: string | null;
  gateway_id: string;
  gateway_name: string;
  connection_kind: "local" | "remote_gateway";
  items: GatewayManagedWorkspace[];
}

export interface AddManagedGatewayWorkspaceRequest {
  gateway_connection_id?: string | null;
  root_path: string;
  name?: string | null;
  create_directory?: boolean;
}

export interface GatewayInboundPeer {
  connection_id: string;
  peer_gateway_id: string;
  credential_expires_at: string;
}

export interface GatewayInboundWorkspace {
  workspace_id: string;
  name: string;
  root_path: string;
  status: "ready" | "offline";
  managed: boolean;
  system_default: boolean;
}

export interface GatewayInboundAccessList {
  gateway_id: string;
  peers: GatewayInboundPeer[];
  items: GatewayInboundWorkspace[];
}

export interface GatewayDeviceConnection {
  connection_id: string;
  device_name: string;
  status: "authorized" | "expired";
  credential_expires_at: string;
}

export interface GatewayDeviceConnectionList {
  items: GatewayDeviceConnection[];
}

export interface GatewayDeviceAccessAddress {
  url: string;
  label: string;
  is_loopback: boolean;
}

export interface GatewayDeviceAccessAddressList {
  items: GatewayDeviceAccessAddress[];
}

export interface GatewayDeviceConnectionInfo {
  gateway_url: string;
  federation_token: string;
  request_header: string;
  manifest_url: string;
  workspaces_url: string;
}

export interface CreatedGatewayDeviceConnection {
  connection: GatewayDeviceConnection;
  connection_info: GatewayDeviceConnectionInfo;
}

export interface GatewayRuntimeBlocker {
  kind: "job" | "tool" | "background_task";
  resource_id: string;
  session_id: string;
  status: string;
  detail?: string | null;
}

export interface GatewayRuntimeRestartResult {
  workspace_id: string;
  status: "restarted" | "blocked";
  forced: boolean;
  blockers: GatewayRuntimeBlocker[];
  workspaces: GatewayWorkspaceList;
}

export interface GatewayRuntimeStateResult {
  workspace_id: string;
  status: "started" | "stopped" | "blocked";
  blockers: GatewayRuntimeBlocker[];
  workspaces: GatewayWorkspaceList;
}

export interface GatewayHealth {
  status: "ok";
  active_workspace_id: string | null;
  process_id: number;
  development_restart_available: boolean;
}

export interface DevelopmentRuntimeRestartResult {
  status: "scheduled";
  previous_process_id: number;
  helper_process_id: number;
  delay_ms: number;
}

interface AddSshGatewayWorkspaceRequestBase {
  name?: string | null;
  remote_gateway_port: number;
}

export interface AddSshGatewayWorkspaceFromWorkspaceRequest
  extends AddSshGatewayWorkspaceRequestBase {
  connection_workspace_id: string;
}

export interface AddSshGatewayWorkspaceFromConfigRequest
  extends AddSshGatewayWorkspaceRequestBase {
  ssh_config_host: string;
}

export interface AddSshGatewayWorkspaceManualRequest
  extends AddSshGatewayWorkspaceRequestBase {
  host: string;
  port: number;
  username: string;
  private_key_path: string;
}

export type AddSshGatewayWorkspaceRequest =
  | AddSshGatewayWorkspaceFromWorkspaceRequest
  | AddSshGatewayWorkspaceFromConfigRequest
  | AddSshGatewayWorkspaceManualRequest;

export interface UpdateGatewayWorkspaceRequest {
  name?: string;
  parent_workspace_id?: string | null;
}

export interface ReorderGatewayWorkspacesRequest {
  workspace_ids: string[];
}

export interface WebUiMainAreaRatios {
  agent_sessions: number;
  chat: number;
  workspace_preview: number;
  auxiliary: number;
}

export interface WebUiLayoutSettings {
  workbench_view?: "sessions" | "gateway" | null;
  agent_sessions_panel_open?: boolean | null;
  auxiliary_visible?: boolean | null;
  auxiliary_tab?: "changes" | "files" | "automation" | "resources" | null;
  main_area_ratios?: WebUiMainAreaRatios | null;
  workspace_preview_visible?: boolean | null;
  workspace_preview_maximized?: boolean | null;
  workspace_preview_file_paths?: string[] | null;
  workspace_preview_active_file_path?: string | null;
  customizations_collapsed?: boolean | null;
  customizations_height?: number | null;
  content_view?:
    | "default"
    | "events"
    | "requests"
    | "changes"
    | "resources"
    | "agent"
    | null;
  pending_message_default_action?: "steering" | "queued" | null;
}

export interface WebUiSessionSidebarSettings {
  filter_mode: "all" | "current" | "attachments" | "agent" | "named";
  sort_mode: "created" | "updated";
  grouping_mode: "workspace" | "time";
  workspace_group_capped: boolean;
  collapsed_workspace_ids: string[];
  collapsed_session_ids: string[];
  expanded_root_tree_ids: string[];
  collapsed_section_ids: string[];
}

export interface WebUiWorkspaceFileTreeSettings {
  expanded_paths_by_workspace: Record<string, string[]>;
}

export interface WebUiGatewayConsoleSettings {
  view: "routing" | "managed";
}

export interface GatewayThemeBackground {
  type: "remote" | "gateway_asset";
  url?: string | null;
  asset_id?: string | null;
  position: string;
  size: string;
  repeat: "no-repeat" | "repeat" | "repeat-x" | "repeat-y" | "space" | "round";
  appearance: "immersive" | "theme";
  overlay: string;
}

export interface ResolvedGatewayTheme {
  id: string;
  label: string;
  color_scheme: "light" | "dark";
  tokens: Record<`--bt-${string}`, string>;
  background_image_url?: string | null;
}

export interface GatewayThemeOption {
  id: string;
  label: string;
  extends: "warm" | "green" | "blue";
  source: "builtin" | "gateway_config";
  preview_tokens: Record<string, string>;
  background_image_url?: string | null;
}

export interface GatewayThemeCatalog {
  current_theme_id: string;
  items: GatewayThemeOption[];
  current_theme: ResolvedGatewayTheme;
}

export interface GatewayUiAsset {
  asset_id: string;
  original_filename: string;
  content_type: string;
  size: number;
  sha256: string;
  imported_at: string;
  url: string;
  referenced_theme_ids: string[];
}

export interface WebUiThemeSettings {
  theme_id: string;
  background?: GatewayThemeBackground | null;
  resolved_theme?: ResolvedGatewayTheme | null;
}

export interface WebUiSettings {
  layout: WebUiLayoutSettings;
  session_sidebar: WebUiSessionSidebarSettings;
  workspace_file_tree: WebUiWorkspaceFileTreeSettings;
  gateway_console: WebUiGatewayConsoleSettings;
  theme: WebUiThemeSettings;
  recent_local_workspace_paths: string[];
}

export interface WebUiSettingsUpdate {
  layout?: WebUiLayoutSettings | null;
  session_sidebar?: Partial<WebUiSessionSidebarSettings> | null;
  workspace_file_tree?: Partial<WebUiWorkspaceFileTreeSettings> | null;
  gateway_console?: Partial<WebUiGatewayConsoleSettings> | null;
  theme?: Partial<WebUiThemeSettings> | null;
  recent_local_workspace_paths?: string[] | null;
}

export interface GatewayDirectoryEntry {
  name: string;
  path: string;
}

export interface GatewayDirectoryList {
  path: string;
  parent_path?: string | null;
  home_path: string;
  entries: GatewayDirectoryEntry[];
  truncated: boolean;
  limit: number;
}

export interface WorkspaceNavigationNode {
  node_id: string;
  kind: "workspace_folder" | "workspace_ref";
  name: string;
  parent_node_id?: string | null;
  workspace_id?: string | null;
  position: number;
}

export interface WorkspaceNavigationTree {
  revision: string;
  nodes: WorkspaceNavigationNode[];
}

export interface SessionCatalogNode {
  node_id: string;
  kind: "folder" | "session";
  name: string;
  parent_node_id?: string | null;
  session_id?: string | null;
  folder_id?: string | null;
  has_children: boolean;
  storage_relative_path?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface SessionCatalogPage {
  revision: string;
  parent_node_id?: string | null;
  items: SessionCatalogNode[];
  cursor?: string | null;
  total: number;
}

export interface GatewaySessionSearchMatch {
  workspace_id: string;
  workspace_name: string;
  node_id: string;
  node_kind: "workspace_folder" | "workspace" | "folder" | "session";
  name: string;
  session_id?: string | null;
  relative_path: string;
  storage_relative_path?: string | null;
  breadcrumb_names: string[];
  breadcrumb_node_ids: string[];
}

export interface GatewaySessionSearchWorkspaceStatus {
  workspace_id: string;
  workspace_name: string;
  status: "available" | "stale" | "unavailable";
  error?: string | null;
}

export interface GatewaySessionSearchResults {
  items: GatewaySessionSearchMatch[];
  workspaces: GatewaySessionSearchWorkspaceStatus[];
  total: number;
}

export type GeneratorSessionStrategyMode =
  | "new_per_run"
  | "continue_existing"
  | "fork_new_and_report_back";

export interface SessionGeneratorDefinition {
  generator_id: string;
  name: string;
  enabled: boolean;
  status: "ready" | "paused" | "blocked";
  status_reason?: string | null;
  revision: number;
  trigger: {
    type: "manual" | "interval" | "cron";
    expression?: string | null;
    interval_seconds?: number | null;
    timezone: string;
  };
  placement: {
    kind: "workspace" | "session" | "session_folder";
    workspace_id: string;
    session_id?: string | null;
    folder_id?: string | null;
  };
  execution_workspace_id: string;
  session_strategy: {
    mode: GeneratorSessionStrategyMode;
    target?: { workspace_id: string; session_id: string } | null;
    concurrency: "queue";
    report_back: "none" | "link" | "summary" | "summary_and_link" | "full" | "continue_agent";
  };
  naming: { title_template: string; path_template: string[] };
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface SessionGeneratorList {
  revision: string;
  items: SessionGeneratorDefinition[];
}

export interface GenerationRun {
  run_id: string;
  generator_id: string;
  idempotency_key: string;
  status: "planned" | "dispatching" | "running" | "reporting" | "completed" | "partial" | "failed" | "cancelled" | "skipped";
  outputs: Array<{
    kind: "session";
    workspace_id: string;
    session_id: string;
    title?: string | null;
    navigation_path: string[];
    storage_relative_path?: string | null;
  }>;
  execution_workspace_id?: string | null;
  message_id?: string | null;
  job_id?: string | null;
  report_back_job_id?: string | null;
  error?: string | null;
}

export interface GenerationRunList {
  items: GenerationRun[];
}

export interface GeneratorPlacementPreview {
  preview_kind: "logical_physical_path_template";
  title: string;
  path_segments: string[];
  session_path_segment: string;
  relative_path: string;
}

export interface SshConnectionOption {
  connection_id: string;
  source: "boxteam" | "ssh_config";
  label: string;
  host: string;
  port: number;
  username: string;
  workspace_id?: string | null;
  ssh_config_host?: string | null;
  initial_path?: string | null;
}

export interface SshConnectionOptionList {
  items: SshConnectionOption[];
}

export type SessionResourceKind = SessionResourceDTO["kind"];
export type SessionResourceAction = NonNullable<SessionResourceDTO["available_actions"]>[number];
