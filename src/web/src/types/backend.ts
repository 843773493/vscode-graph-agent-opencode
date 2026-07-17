// 该文件是前端业务类型适配层，封装后端实际返回结构。
// 本目录业务代码统一从这里导入类型；后端 DTO 直接别名到生成文件，避免手写协议漂移。

export type { AgentDTO as Agent } from "./gen/agent";
import type { LLMRequestLogRecordDTO } from "./gen/llm_request_log";
export type {
  AgentStateMessagesDTO as AgentStateMessages,
  AttachmentRef,
  MessageDTO as Message,
  MessageReplayAccepted,
  MessageReplayRequest,
  MessageRunAccepted,
  MessageRunRequest,
  RunOptions,
} from "./gen/message";
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
export type {
  WorkspaceDTO as WorkspaceInfo,
  WorkspaceFileContentDTO as WorkspaceFileContent,
  WorkspaceFileListDTO as WorkspaceFileList,
  WorkspaceFileNodeDTO as WorkspaceFileNode,
} from "./gen/workspace";

export type LLMRequestLogRecord = Omit<
  LLMRequestLogRecordDTO,
  "request" | "response"
> & {
  request: Record<string, unknown>;
  response: Record<string, unknown>;
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
  name: string;
  root_path: string;
  backend_url: string;
  connection_kind: "local" | "ssh";
  status: "ready" | "offline";
  active: boolean;
  managed: boolean;
  removable: boolean;
  system_default: boolean;
  remote: GatewaySshRemoteDetails;
  services: Record<string, GatewayServiceStatus>;
  connection_error?: string | null;
  checked_at: string;
}

export interface GatewaySshRemoteDetails {
  [key: string]: unknown;
  host?: string;
  port?: number;
  username?: string;
  remote_backend_host?: string;
  remote_backend_port?: number;
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

export interface GatewayHealth {
  status: "ok";
  active_workspace_id: string | null;
}

export interface AddLocalGatewayWorkspaceRequest {
  root_path: string;
  name?: string | null;
  backend_url?: string | null;
}

interface AddSshGatewayWorkspaceRequestBase {
  name?: string | null;
  remote_workspace_path: string;
}

export interface AddSshGatewayWorkspaceFromWorkspaceRequest
  extends AddSshGatewayWorkspaceRequestBase {
  connection_workspace_id: string;
}

export interface AddSshGatewayWorkspaceFromConfigRequest
  extends AddSshGatewayWorkspaceRequestBase {
  ssh_config_host: string;
}

export type AddSshGatewayWorkspaceRequest =
  | AddSshGatewayWorkspaceFromWorkspaceRequest
  | AddSshGatewayWorkspaceFromConfigRequest;

export interface RenameGatewayWorkspaceRequest {
  name: string;
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
}

export interface WebUiSettings {
  layout: WebUiLayoutSettings;
  recent_local_workspace_paths: string[];
}

export interface WebUiSettingsUpdate {
  layout?: WebUiLayoutSettings | null;
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
