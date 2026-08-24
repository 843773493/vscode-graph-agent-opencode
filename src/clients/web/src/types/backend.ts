// 该文件是前端业务类型适配层，封装后端实际返回结构。
// 本目录业务代码统一从这里导入类型；后端 DTO 直接别名到生成文件，避免手写协议漂移。

export type { AgentDTO as Agent } from "./gen/agent";
export type { JobDTO as Job, JobStatus } from "./gen/job";
export type {
  NodeDebugActionRequest,
  NodeDebugActionRecordDTO as NodeDebugActionRecord,
  NodeDebugBreakpointDTO as NodeDebugBreakpoint,
  NodeDebugCapabilitiesDTO as NodeDebugCapabilities,
  NodeDebugConfigurationDTO as NodeDebugConfiguration,
  NodeDebugConfigurationSummaryDTO as NodeDebugConfigurationSummary,
  NodeDebugEvaluationDTO as NodeDebugEvaluation,
  NodeDebugLaunchProfileDTO as NodeDebugLaunchProfile,
  NodeDebugStackFrameDTO as NodeDebugStackFrame,
  NodeDebugStartRequest,
  NodeDebugStateDTO as NodeDebugState,
  NodeDebugVariableDTO as NodeDebugVariable,
} from "./gen/node_debug";
export type {
  ControlAction,
  JobControlRequest,
  JobControlResponseDTO as JobControlResponse,
} from "./gen/job";
import type { LLMRequestLogRecordDTO } from "./gen/llm_request_log";
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
  PendingRequestPolicyUpdateRequest,
  PendingRequestUpdateRequest,
} from "./gen/pending_request";
export type DeliveryPolicy =
  | "after_turn"
  | "after_tool_result"
  | "after_interrupt";

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
import type {
  ActivateGatewayWorkspaceResultDTO,
  AddLocalWorkspaceRequest as GeneratedAddLocalWorkspaceRequest,
  AddRemoteGatewayRequest as GeneratedAddRemoteGatewayRequest,
  CreateFederationManagedWorkspaceRequest as GeneratedCreateFederationManagedWorkspaceRequest,
  CreateGatewayGuestRequest as GeneratedCreateGatewayGuestRequest,
  CreateGatewayManagedWorkspaceRequest as GeneratedCreateGatewayManagedWorkspaceRequest,
  CreateGatewayUserRequest as GeneratedCreateGatewayUserRequest,
  DevelopmentRuntimeRestartDTO,
  GatewayConfigReloadStatusDTO,
  GatewayConfigSourceDTO,
  GatewayConfigSourcesDTO,
  GatewayDiagnosticLogDTO,
  GatewayDiagnosticWorkspaceDTO,
  GatewayDiagnosticsDTO,
  GatewayDirectoryEntryDTO,
  GatewayDirectoryListDTO,
  GatewayHealthDTO,
  GatewayInboundAccessListDTO,
  GatewayInboundPeerDTO,
  GatewayInboundWorkspaceDTO,
  GatewayManagedWorkspaceDTO,
  GatewayManagedWorkspaceListDTO,
  GatewayRemoteConnectionSummaryDTO,
  GatewayRuntimeBlockerDTO,
  GatewayRuntimeRestartResultDTO,
  GatewayRuntimeStateResultDTO,
  GatewayServiceStatusDTO,
  GatewayThemeBackgroundDTO,
  GatewayThemeCatalogDTO,
  GatewayThemeOptionDTO,
  GatewayUIAssetDTO,
  GatewayUIAssetListDTO,
  GatewayUserAccessDTO,
  GatewayUserDTO,
  GatewayUserLeaseDTO,
  GatewayUserListDTO,
  GatewayUserViewStateDTO,
  GatewayUserViewStateUpdateRequest as GeneratedGatewayUserViewStateUpdateRequest,
  GatewayWorkspaceDTO,
  GatewayWorkspaceListDTO,
  PortForwardDTO,
  PortForwardListDTO,
  ReorderGatewayWorkspacesRequest as GeneratedReorderGatewayWorkspacesRequest,
  ResolvedGatewayThemeDTO,
  SshConnectionOptionDTO,
  SshConnectionOptionListDTO,
  UpdateGatewayWorkspaceRequest as GeneratedUpdateGatewayWorkspaceRequest,
  WebUIGatewayConsoleSettingsDTO,
  WebUILayoutSettingsDTO,
  WebUIMainAreaRatiosDTO,
  WebUISessionSidebarSettingsDTO,
  WebUISettingsDTO,
  WebUISettingsUpdateDTO,
  WebUIThemeSettingsDTO,
  WebUIWorkspaceBottomPanelSettingsDTO,
  WebUIWorkspaceFileTreeSettingsDTO,
} from "./gen/gateway";
import type {
  GatewayResourceDTO,
  GatewayResourceListDTO,
  GatewayResourceScopeErrorDTO,
  GatewaySessionSearchMatchDTO,
  GatewaySessionSearchResultsDTO,
  GatewaySessionSearchWorkspaceStatusDTO,
  GenerationOutputDTO,
  GenerationRunDTO,
  GenerationRunListDTO,
  GeneratorDefinitionDTO,
  GeneratorDefinitionCreateRequest as GeneratedGeneratorDefinitionCreateRequest,
  GeneratorDefinitionListDTO,
  GeneratorDefinitionUpdateRequest as GeneratedGeneratorDefinitionUpdateRequest,
  GeneratorManualRunRequest as GeneratedGeneratorManualRunRequest,
  GeneratorPlacementPreviewDTO,
  GeneratorPlacementPreviewRequest as GeneratedGeneratorPlacementPreviewRequest,
  GeneratorSessionStrategyDTO,
  SessionLocatorDTO,
  WorkspaceNavigationNodeDTO,
  WorkspaceFolderCreateRequest as GeneratedWorkspaceFolderCreateRequest,
  WorkspaceNavigationNodeUpdateRequest as GeneratedWorkspaceNavigationNodeUpdateRequest,
  WorkspaceNavigationPlacementRequest as GeneratedWorkspaceNavigationPlacementRequest,
  WorkspaceNavigationTreeDTO,
} from "./gen/gateway_control";
export type { TraceEventDTO as SessionStreamEvent } from "./gen/trace";
export type {
  SessionTurnBootstrapDTO as SessionTurnBootstrap,
  StaleTurnCursorErrorDTO as StaleTurnCursorError,
  StaleTurnReferenceErrorDTO as StaleTurnReferenceError,
  TurnAttachmentDTO as TurnAttachment,
  TurnActivityStatsDTO as TurnActivityStats,
  TurnDetailBatchDTO as TurnDetailBatch,
  TurnDetailBatchRequest,
  TurnDetailDTO as TurnDetail,
  TurnHistoryLoadRequest,
  TurnHistoryPageDTO as TurnHistoryPage,
  TurnJobSummaryDTO as TurnJobSummary,
  TurnPageDTO as TurnPage,
  TurnSummaryDTO as TurnSummary,
  TurnResponsePartDTO as TurnResponsePart,
  TurnResponseSourceDTO as TurnResponseSource,
  TurnThinkingBlockDTO as TurnThinkingBlock,
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

export interface SessionActivity {
  event_seq: number;
  event_id: string;
  session_id: string;
  status: "completed" | "failed" | "cancelled" | string;
  summary: string;
  occurred_at: string;
}

type DeepRequired<T> = T extends null
  ? null
  : T extends readonly (infer Item)[]
    ? DeepRequired<Item>[]
    : T extends object
      ? { [Key in keyof T]-?: DeepRequired<Exclude<T[Key], undefined>> }
      : T;

export type AddLocalWorkspaceRequest = GeneratedAddLocalWorkspaceRequest;
export type AddRemoteGatewayRequest = GeneratedAddRemoteGatewayRequest;
export type CreateFederationManagedWorkspaceRequest =
  GeneratedCreateFederationManagedWorkspaceRequest;
export type CreateGatewayGuestRequest = GeneratedCreateGatewayGuestRequest;
export type CreateGatewayUserRequest = GeneratedCreateGatewayUserRequest;
export type AcquireGatewayUserRequest = import("./gen/gateway").AcquireGatewayUserRequest;
export type GatewayUserViewStateUpdateRequest =
  GeneratedGatewayUserViewStateUpdateRequest;
type GeneratorNamingRequest = Omit<
  NonNullable<GeneratedGeneratorDefinitionCreateRequest["naming"]>,
  "path_template"
> & {
  path_template?: string[];
};
export type GeneratorDefinitionCreateRequest = Omit<
  GeneratedGeneratorDefinitionCreateRequest,
  "naming"
> & {
  naming?: GeneratorNamingRequest;
};
export type GeneratorDefinitionUpdateRequest = Omit<
  GeneratedGeneratorDefinitionUpdateRequest,
  "naming"
> & {
  naming?: GeneratorNamingRequest | null;
};
export type GeneratorManualRunRequest = GeneratedGeneratorManualRunRequest;
export type GeneratorPlacementPreviewRequest = Omit<
  GeneratedGeneratorPlacementPreviewRequest,
  "naming"
> & {
  naming: GeneratorNamingRequest;
};
export type WorkspaceFolderCreateRequest = GeneratedWorkspaceFolderCreateRequest;
export type WorkspaceNavigationNodeUpdateRequest =
  GeneratedWorkspaceNavigationNodeUpdateRequest;
export type WorkspaceNavigationPlacementRequest =
  GeneratedWorkspaceNavigationPlacementRequest;

export type GatewayWorkspace = Omit<
  Required<GatewayWorkspaceDTO>,
  "parent_workspace_id" | "runtime_action" | "config_reload" | "connection_error"
> & {
  parent_workspace_id?: string | null;
  runtime_action?: GatewayWorkspaceDTO["runtime_action"];
  config_reload?: GatewayConfigReloadStatus;
  connection_error?: string | null;
};
export type GatewayUserLease = Required<GatewayUserLeaseDTO>;
export type GatewayUser = Omit<Required<GatewayUserDTO>, "lease"> & {
  lease: GatewayUserLease;
};
export type GatewayUserList = Omit<Required<GatewayUserListDTO>, "items"> & {
  items: GatewayUser[];
};
export type GatewayUserAccess = Required<GatewayUserAccessDTO>;
export type GatewayUserViewState = DeepRequired<GatewayUserViewStateDTO>;
export type GatewayConfigReloadStatus = Omit<
  Required<GatewayConfigReloadStatusDTO>,
  "healthy" | "revision" | "reason" | "last_error" | "error"
> & {
  healthy?: boolean | null;
  revision?: string | null;
  reason?: GatewayConfigReloadStatusDTO["reason"];
  last_error?: string | null;
  error?: string | null;
};
export type GatewayConfigSource = GatewayConfigSourceDTO;
export type GatewayConfigSources = GatewayConfigSourcesDTO;
export type GatewayRemoteConnectionSummary = GatewayRemoteConnectionSummaryDTO;
export type GatewayServiceStatus = GatewayServiceStatusDTO;
export type GatewayWorkspaceList = Omit<
  Required<GatewayWorkspaceListDTO>,
  "items"
> & {
  items: GatewayWorkspace[];
};

export type GatewayResourceScopeError = GatewayResourceScopeErrorDTO;
export type GatewayResourceItem = Omit<GatewayResourceDTO, "resource"> & {
  resource: SessionResource;
};
export type GatewayResourceList = Omit<GatewayResourceListDTO, "items" | "errors"> & {
  items: GatewayResourceItem[];
  errors: GatewayResourceScopeError[];
};
export type GatewayDiagnosticLog = DeepRequired<GatewayDiagnosticLogDTO>;
export type GatewayDiagnosticWorkspace = DeepRequired<GatewayDiagnosticWorkspaceDTO>;
export type GatewayDiagnostics = DeepRequired<GatewayDiagnosticsDTO>;

export type GatewayPortForwardProtocol = PortForwardDTO["protocol"];
export type GatewayPortForwardStatus = PortForwardDTO["status"];
export type GatewayPortForward = PortForwardDTO;
export type GatewayPortForwardList = PortForwardListDTO;
export type CreateGatewayPortForwardRequest = import("./gen/gateway").CreatePortForwardRequest;
export type ChangeGatewayPortForwardLocalPortRequest =
  import("./gen/gateway").ChangePortForwardLocalPortRequest;
export type ChangeGatewayPortForwardLabelRequest =
  import("./gen/gateway").ChangePortForwardLabelRequest;

export type GatewayManagedWorkspace = Required<GatewayManagedWorkspaceDTO>;
export type GatewayManagedWorkspaceList = Omit<
  Required<GatewayManagedWorkspaceListDTO>,
  "gateway_connection_id" | "items"
> & {
  gateway_connection_id?: string | null;
  items: GatewayManagedWorkspace[];
};
export type AddManagedGatewayWorkspaceRequest =
  GeneratedCreateGatewayManagedWorkspaceRequest;
export type GatewayInboundPeer = Required<GatewayInboundPeerDTO>;
export type GatewayInboundWorkspace = Required<GatewayInboundWorkspaceDTO>;
export type GatewayInboundAccessList = Omit<
  Required<GatewayInboundAccessListDTO>,
  "peers" | "items"
> & {
  peers: GatewayInboundPeer[];
  items: GatewayInboundWorkspace[];
};

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

export type GatewayRuntimeBlocker = Required<GatewayRuntimeBlockerDTO>;
export type GatewayRuntimeRestartResult = Omit<
  Required<GatewayRuntimeRestartResultDTO>,
  "blockers" | "workspaces"
> & {
  blockers: GatewayRuntimeBlocker[];
  workspaces: GatewayWorkspaceList;
};
export type GatewayRuntimeStateResult = Omit<
  Required<GatewayRuntimeStateResultDTO>,
  "blockers" | "workspaces"
> & {
  blockers: GatewayRuntimeBlocker[];
  workspaces: GatewayWorkspaceList;
};
export type GatewayHealth = GatewayHealthDTO;
export type DevelopmentRuntimeRestartResult = DevelopmentRuntimeRestartDTO;

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

export type UpdateGatewayWorkspaceRequest = GeneratedUpdateGatewayWorkspaceRequest;
export type ReorderGatewayWorkspacesRequest = GeneratedReorderGatewayWorkspacesRequest;

export type WebUiMainAreaRatios = Required<WebUIMainAreaRatiosDTO>;

export type WebUiBottomPanelTab = "terminal" | "output" | "ports" | "automation";

/** 仅用于读取旧版本设置；新的底部面板状态统一使用 output。 */
export type LegacyWebUiBottomPanelTab = WebUiBottomPanelTab | "gateway";

export type WebUiWorkspaceBottomPanelSettings =
  WebUIWorkspaceBottomPanelSettingsDTO;

export type WebUiLayoutSettings = Omit<
  WebUILayoutSettingsDTO,
  "auxiliary_tab_order" | "main_area_ratios" | "workspace_preview_file_paths"
> & {
  main_area_ratios?: WebUiMainAreaRatios | null;
  auxiliary_tab_order?: Array<
    "changes" | "files" | "automation" | "resources" | "debug"
  > | null;
  workspace_preview_file_paths?: string[] | null;
};

export type WebUiSessionSidebarSettings =
  Required<WebUISessionSidebarSettingsDTO>;

export type WebUiWorkspaceFileTreeSettings =
  Required<WebUIWorkspaceFileTreeSettingsDTO>;

export type WebUiGatewayConsoleSettings = WebUIGatewayConsoleSettingsDTO;

export type GatewayThemeBackground = Omit<
  Required<GatewayThemeBackgroundDTO>,
  "url" | "asset_id"
> & {
  url?: string | null;
  asset_id?: string | null;
};

export type ResolvedGatewayTheme = ResolvedGatewayThemeDTO;

export type GatewayThemeOption = GatewayThemeOptionDTO;

export type GatewayThemeCatalog = GatewayThemeCatalogDTO;

export type GatewayUiAsset = Required<GatewayUIAssetDTO>;
export type GatewayUiAssetList = Omit<
  Required<GatewayUIAssetListDTO>,
  "items"
> & {
  items: GatewayUiAsset[];
};

export type WebUiThemeSettings = Required<WebUIThemeSettingsDTO>;

export type WebUiSettings = Omit<
  Required<WebUISettingsDTO>,
  "layout" | "session_sidebar" | "workspace_file_tree" | "gateway_console" | "theme"
> & {
  layout: WebUiLayoutSettings;
  session_sidebar: WebUiSessionSidebarSettings;
  workspace_file_tree: WebUiWorkspaceFileTreeSettings;
  gateway_console: WebUiGatewayConsoleSettings;
  theme: WebUiThemeSettings;
};

export type WebUiSettingsUpdate = Omit<
  WebUISettingsUpdateDTO,
  "layout" | "session_sidebar" | "workspace_file_tree" | "gateway_console" | "theme"
> & {
  layout?: WebUiLayoutSettings | null;
  session_sidebar?: Partial<WebUiSessionSidebarSettings> | null;
  workspace_file_tree?: Partial<WebUiWorkspaceFileTreeSettings> | null;
  gateway_console?: Partial<WebUiGatewayConsoleSettings> | null;
  theme?: Partial<WebUiThemeSettings> | null;
};

export type GatewayDirectoryEntry = Required<GatewayDirectoryEntryDTO>;

export type GatewayDirectoryList = Omit<
  Required<GatewayDirectoryListDTO>,
  "parent_path"
> & {
  parent_path?: string | null;
};

export type WorkspaceNavigationNode = DeepRequired<WorkspaceNavigationNodeDTO>;

export type WorkspaceNavigationTree = Omit<
  DeepRequired<WorkspaceNavigationTreeDTO>,
  "nodes"
> & {
  nodes: WorkspaceNavigationNode[];
};

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

export type GatewaySessionSearchMatch = DeepRequired<GatewaySessionSearchMatchDTO>;

export type GatewaySessionSearchWorkspaceStatus =
  DeepRequired<GatewaySessionSearchWorkspaceStatusDTO>;

export type GatewaySessionSearchResults = Omit<
  DeepRequired<GatewaySessionSearchResultsDTO>,
  "items" | "workspaces"
> & {
  items: GatewaySessionSearchMatch[];
  workspaces: GatewaySessionSearchWorkspaceStatus[];
};

export type GeneratorSessionStrategyMode = NonNullable<
  GeneratorSessionStrategyDTO["mode"]
>;

export type SessionGeneratorDefinition = Omit<
  GeneratorDefinitionDTO,
  "name" | "enabled" | "status" | "revision" | "trigger" | "placement" | "session_strategy" | "naming" | "config"
> & {
  name: string;
  enabled: boolean;
  status: NonNullable<GeneratorDefinitionDTO["status"]>;
  revision: number;
  trigger: NonNullable<GeneratorDefinitionDTO["trigger"]>;
  placement: NonNullable<GeneratorDefinitionDTO["placement"]>;
  session_strategy: Omit<
    NonNullable<GeneratorDefinitionDTO["session_strategy"]>,
    "mode"
  > & {
    mode: GeneratorSessionStrategyMode;
  };
  naming: Required<NonNullable<GeneratorDefinitionDTO["naming"]>>;
  config: Record<string, unknown>;
};

export type SessionGeneratorList = Omit<GeneratorDefinitionListDTO, "items"> & {
  items: SessionGeneratorDefinition[];
};

type GenerationOutput = Omit<GenerationOutputDTO, "kind" | "navigation_path"> & {
  kind: "session";
  navigation_path: string[];
};

export type GenerationRun = Omit<GenerationRunDTO, "outputs"> & {
  outputs: GenerationOutput[];
};

export type GenerationRunList = Omit<GenerationRunListDTO, "items"> & {
  items: GenerationRun[];
};

export type GeneratorPlacementPreview = Required<GeneratorPlacementPreviewDTO>;

export type SshConnectionOption = SshConnectionOptionDTO;

export type SshConnectionOptionList = Omit<
  Required<SshConnectionOptionListDTO>,
  "items"
> & {
  items: SshConnectionOption[];
};

export type SessionResourceKind = SessionResourceDTO["kind"];
export type SessionResourceAction = NonNullable<SessionResourceDTO["available_actions"]>[number];
