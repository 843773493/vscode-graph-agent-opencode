// Web 端公开业务协议的 TypeScript 适配类型。
// 结构和字段名来自 .proto 生成绑定；这里只处理 JSON API 的字符串枚举、可空时间和动态对象。
import type * as WorkspaceProtocol from "./protocol_generated/boxteam/workspace/v2/public";
import type { AttachmentRef as GeneratedAttachmentRef } from "./protocol_generated/boxteam/workspace/v2/message";
import type { TraceEventDTO } from "../protocol/jsonTypes";

export type JsonObject = Record<string, unknown>;
export type DeliveryPolicy = "after_turn" | "after_tool_result" | "after_interrupt";
export type ConfigSourceLayer =
  | "inline"
  | "user"
  | "user_local"
  | "workspace"
  | "runtime_override";
export type ConfigSourceDTO = Omit<
  WorkspaceProtocol.ConfigSourceDTO,
  | "uri"
  | "layer"
  | "source_key"
  | "presence"
  | "layer_revision"
  | "layer_digest"
  | "source_generation"
> & {
  uri: string | null;
  layer: ConfigSourceLayer;
  source_key: string | null;
  presence: "present" | "absent";
  layer_revision: number | null;
  layer_digest: string | null;
  source_generation: number | null;
};
export type ConfigSourcesDTO = Omit<
  WorkspaceProtocol.ConfigSourcesDTO,
  "schema_uri" | "sources"
> & {
  schema_uri: string | null;
  sources: ConfigSourceDTO[];
};

export type JobStatus =
  | "accepted"
  | "queued"
  | "running"
  | "streaming"
  | "waiting_input"
  | "paused"
  | "interrupt_pending"
  | "cancelling"
  | "completed"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "timed_out";

export type RunMode = "single_agent" | "multi_agent";
export type MessageRole = "user" | "assistant" | "system" | "tool";
export type StepStatus = "pending" | "running" | "completed" | "failed" | "skipped" | "cancelled";
export type ControlScope = "job" | "agent" | "step";
export type ControlAction =
  | "pause"
  | "resume"
  | "cancel"
  | "skip"
  | "replace_instruction"
  | "append_instruction"
  | "retry";

export type ChildThreadStatus = "idle" | "pending" | "running" | "completed" | "failed";

export function parseChildThreadStatus(value: unknown): ChildThreadStatus {
  if (value === "idle" || value === "pending" || value === "running" || value === "completed" || value === "failed") {
    return value;
  }
  throw new Error(`child thread status 协议值无效: ${String(value)}`);
}

export type SessionForkMode = "view_prefix_fork" | "context_fork" | "full_rollout_copy";
export type SessionForkCreationKind = "new_session_main" | "new_child_thread";
export type SessionForkResourceAction = "reuse_reference" | "retain_reference" | "copy_snapshot" | "omit" | "unsupported";
export type SessionForkOwnerReference = Omit<WorkspaceProtocol.SessionForkOwnerReference, "kind"> & {
  kind: "terminal" | "browser";
};
type SessionForkLiveResource = Omit<WorkspaceProtocol.WorkspaceResourceDTO, "source_thread" | "unattached_since" | "reclaim_at" | "error"> & {
  source_thread: NonNullable<WorkspaceProtocol.WorkspaceResourceDTO["source_thread"]>;
  unattached_since?: string | null;
  reclaim_at?: string | null;
  error?: string | null;
};
export type SessionForkResourceChoice = {
  resource_uri: string;
  owner_reference?: null;
  action: SessionForkResourceAction;
} | {
  resource_uri?: null;
  owner_reference: SessionForkOwnerReference;
  action: "reuse_reference" | "omit";
};
export type SessionForkRequest = Omit<WorkspaceProtocol.SessionForkRequest,
  "mode" | "anchor_mode" | "creation_kind" | "relationship" | "resources" | "goal" | "anchor_item_id" | "title"
> & {
  mode: SessionForkMode;
  anchor_mode: "inclusive" | "before";
  creation_kind: SessionForkCreationKind;
  relationship: "detached" | "pinned";
  resources: SessionForkResourceChoice[];
  goal?: WorkspaceProtocol.SessionForkGoalSelection | null;
  anchor_item_id: string | null;
  title?: string | null;
};
export type SessionForkSourcePoint = Omit<WorkspaceProtocol.SessionForkSourcePointDTO,
  "resource_mode" | "resource_anchor_mode" | "anchor_item_id" | "live_resources"
> & {
  anchor_item_id: string | null;
  resource_mode: SessionForkMode;
  resource_anchor_mode: "inclusive" | "before";
  live_resources: SessionForkLiveResource[];
};
/** 顶层 thread_id/thread_uri 是本次 fork 的目标 Thread；session 是其所属 Session，session.thread_id 标识该 Session 的 main Thread。 */
export type SessionForkResult = Omit<WorkspaceProtocol.SessionForkResultDTO,
  "session" | "creation_kind" | "mode" | "resources" | "anchor_item_id" | "goal_id"
> & {
  session: Session;
  creation_kind: SessionForkCreationKind;
  mode: SessionForkMode;
  anchor_item_id: string | null;
  goal_id: string | null;
  resources: Array<Omit<WorkspaceProtocol.SessionForkResourceResult,
    "resource_uri" | "action" | "availability" | "target_uri" | "reason" | "owner_reference" | "owner_resource" | "operation_state" | "committed_revision"
  > & {
    resource_uri?: string | null;
    action: SessionForkResourceAction;
    availability: "available" | "unavailable";
    target_uri: string | null;
    reason: string | null;
    owner_reference?: SessionForkOwnerReference | null;
    owner_resource?: SessionForkLiveResource | null;
    operation_state?: "not_dispatched" | "dispatching" | "committed" | "rejected" | null;
    /** 本次 attach 的历史提交 revision；尚未提交时省略，当前 owner 版本见 owner_resource.owner_revision。 */
    committed_revision?: number | null;
  }>;
};

export type ChildThreadSummary = Omit<
  WorkspaceProtocol.ChildThreadSummaryDTO,
  "created_at"
  | "delegation_id"
  | "role"
  | "subagent_type"
  | "title"
  | "collaboration_state"
  | "admission_state"
  | "status"
  | "runtime"
  | "report_execution_id"
  | "report_status"
> & {
  created_at: string;
  delegation_id?: string | null;
  role?: string | null;
  subagent_type?: string | null;
  title?: string | null;
  collaboration_state?: string | null;
  admission_state?: string | null;
  status: ChildThreadStatus;
  runtime?: Omit<WorkspaceProtocol.ChildThreadRuntimeDTO, "last_activity_at" | "idle_deadline"> & {
    last_activity_at: string | null;
    idle_deadline: string | null;
  } | null;
  report_execution_id?: string | null;
  report_status?: InternalExecutionStatus | null;
};
export type ChildThreadList = Omit<WorkspaceProtocol.ChildThreadListDTO, "items"> & {
  items: ChildThreadSummary[];
};

export type AttachmentRef = Omit<GeneratedAttachmentRef, "name" | "content_type" | "data_url"> & {
  name?: string | null;
  content_type?: string | null;
  data_url?: string | null;
};
export type Agent = Omit<WorkspaceProtocol.AgentDTO, "description"> & {
  description?: string | null;
};
export type ExtensionCatalogBindingAudit = WorkspaceProtocol.ExtensionCatalogBindingAuditDTO;
export type NodeDebugAction =
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

export type NodeDebugNoActionParams = Record<string, never>;
export interface NodeDebugSetBreakpointParams {
  path: string;
  line: number;
  column?: number;
  condition?: string | null;
  hit_condition?: number | null;
  log_message?: string | null;
}
export interface NodeDebugUpdateBreakpointParams {
  breakpoint_id: string;
  path?: string | null;
  line?: number | null;
  column?: number | null;
  condition?: string | null;
  hit_condition?: number | null;
  log_message?: string | null;
}
export interface NodeDebugClearBreakpointParams {
  breakpoint_id: string;
}
export interface NodeDebugEvaluateParams {
  expression: string;
  call_frame_id?: string | null;
}

interface NodeDebugActionRequestBase {
  session_id: string;
  thread_id: string;
}
export type NodeDebugControlActionRequest = NodeDebugActionRequestBase & {
  action: Extract<NodeDebugAction, "continue" | "pause" | "step_over" | "step_into" | "step_out" | "stop">;
  params?: NodeDebugNoActionParams;
};
export type NodeDebugSetBreakpointActionRequest = NodeDebugActionRequestBase & {
  action: "set_breakpoint";
  params: NodeDebugSetBreakpointParams;
};
export type NodeDebugUpdateBreakpointActionRequest = NodeDebugActionRequestBase & {
  action: "update_breakpoint";
  params: NodeDebugUpdateBreakpointParams;
};
export type NodeDebugClearBreakpointActionRequest = NodeDebugActionRequestBase & {
  action: "clear_breakpoint";
  params: NodeDebugClearBreakpointParams;
};
export type NodeDebugEvaluateActionRequest = NodeDebugActionRequestBase & {
  action: "evaluate";
  params: NodeDebugEvaluateParams;
};
export type NodeDebugActionRequest =
  | NodeDebugControlActionRequest
  | NodeDebugSetBreakpointActionRequest
  | NodeDebugUpdateBreakpointActionRequest
  | NodeDebugClearBreakpointActionRequest
  | NodeDebugEvaluateActionRequest;
export type NodeDebugActionCommand =
  | Omit<NodeDebugControlActionRequest, "session_id" | "thread_id">
  | Omit<NodeDebugSetBreakpointActionRequest, "session_id" | "thread_id">
  | Omit<NodeDebugUpdateBreakpointActionRequest, "session_id" | "thread_id">
  | Omit<NodeDebugClearBreakpointActionRequest, "session_id" | "thread_id">
  | Omit<NodeDebugEvaluateActionRequest, "session_id" | "thread_id">;
export type NodeDebugActionRecord = Omit<
  WorkspaceProtocol.NodeDebugActionRecordDTO,
  "actor" | "tool_name" | "tool_call_id" | "extension_catalog_binding" | "result" | "created_at"
> & {
  actor?: "human" | "ai" | "system";
  tool_name?: string | null;
  tool_call_id?: string | null;
  extension_catalog_binding?: ExtensionCatalogBindingAudit | null;
  result?: "success" | "error";
  created_at: string;
};
export type NodeDebugBreakpoint = Omit<
  WorkspaceProtocol.NodeDebugBreakpointDTO,
  "condition" | "hit_condition" | "log_message" | "actual_line" | "inspector_id" | "original_line" | "source_line" | "previous_line" | "next_line" | "source_digest" | "relocation_status" | "relocation_message" | "created_at"
> & {
  condition?: string | null;
  hit_condition?: number | null;
  log_message?: string | null;
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
};
export type NodeDebugBreakpointRequest = Omit<
  WorkspaceProtocol.NodeDebugBreakpointRequest,
  "condition" | "hit_condition" | "log_message"
> & {
  condition?: string | null;
  hit_condition?: number | null;
  log_message?: string | null;
};
export type NodeDebugCapabilities = Omit<WorkspaceProtocol.NodeDebugCapabilitiesDTO, "supported_adapters" | "launch_profiles"> & {
  supported_adapters?: string[];
  launch_profiles?: WorkspaceProtocol.NodeDebugLaunchProfileDTO[];
};
export type NodeDebugConfiguration = Omit<WorkspaceProtocol.NodeDebugConfigurationDTO, "script_path" | "working_directory" | "launch_profile_name" | "args" | "breakpoints" | "created_at" | "updated_at"> & {
  script_path?: string | null;
  working_directory?: string;
  launch_profile_name?: string | null;
  args?: string[];
  breakpoints?: WorkspaceProtocol.NodeDebugConfigurationBreakpointDTO[];
  created_at: string;
  updated_at: string;
};
export type NodeDebugConfigurationSummary = WorkspaceProtocol.NodeDebugConfigurationSummaryDTO;
export type NodeDebugConfigurationActivateRequest = WorkspaceProtocol.NodeDebugConfigurationActivateRequest;
export type NodeDebugConfigurationCopyRequest = Omit<
  WorkspaceProtocol.NodeDebugConfigurationCopyRequest,
  "name" | "source_thread_id" | "target_thread_id"
> & {
  source_thread_id?: string;
  target_thread_id?: string;
  name?: string | null;
};
export type NodeDebugConfigurationCreateRequest = Omit<
  WorkspaceProtocol.NodeDebugConfigurationCreateRequest,
  "script_path" | "launch_profile_name" | "args" | "breakpoints"
> & {
  script_path?: string | null;
  launch_profile_name?: string | null;
  args?: string[];
  breakpoints?: NodeDebugBreakpointRequest[];
};
export type NodeDebugConfigurationImportRequest = Omit<
  WorkspaceProtocol.NodeDebugConfigurationImportRequest,
  "configuration"
> & {
  configuration: NodeDebugConfiguration;
};
export type NodeDebugConfigurationUpdateRequest = Omit<
  WorkspaceProtocol.NodeDebugConfigurationUpdateRequest,
  "script_path" | "launch_profile_name" | "args" | "breakpoints"
> & {
  script_path?: string | null;
  launch_profile_name?: string | null;
  args?: string[];
  breakpoints?: NodeDebugBreakpointRequest[];
};
export type NodeDebugEvaluation = WorkspaceProtocol.NodeDebugEvaluationDTO;
export type NodeDebugLaunchProfile = WorkspaceProtocol.NodeDebugLaunchProfileDTO;
export type NodeDebugStackFrame = WorkspaceProtocol.NodeDebugStackFrameDTO;
export type NodeDebugStartRequest = Omit<
  WorkspaceProtocol.NodeDebugStartRequest,
  "configuration_id" | "working_directory" | "launch_profile_name" | "args" | "breakpoints"
> & {
  configuration_id?: string | null;
  working_directory?: string | null;
  launch_profile_name?: string | null;
  args?: string[];
  breakpoints?: NodeDebugBreakpointRequest[];
};
export type NodeDebugState = Omit<WorkspaceProtocol.NodeDebugStateDTO, "status" | "active_configuration_id" | "active_configuration_name" | "script_path" | "working_directory" | "launch_profile_name" | "pid" | "paused_reason" | "error_message" | "call_stack" | "last_stopped_frame" | "breakpoints" | "last_evaluation" | "evaluations" | "actions" | "source_changed_paths"> & {
  status:
    | "idle"
    | "starting"
    | "running"
    | "paused"
    | "stopping"
    | "exited"
    | "failed"
    | "reconcile_required";
  active_configuration_id?: string | null;
  active_configuration_name?: string | null;
  script_path?: string | null;
  working_directory?: string | null;
  launch_profile_name?: string | null;
  pid?: number | null;
  paused_reason?: string | null;
  error_message?: string | null;
  call_stack?: NodeDebugStackFrame[];
  last_stopped_frame?: NodeDebugStackFrame | null;
  breakpoints?: NodeDebugBreakpoint[];
  last_evaluation?: NodeDebugEvaluation | null;
  evaluations?: NodeDebugEvaluation[];
  actions?: NodeDebugActionRecord[];
  source_changed_paths?: string[];
};
export type JobDispatchStatus = "queued" | "running" | "completed" | "failed" | "cancelled" | "timed_out";
export type JobDispatchSnapshot = Omit<
  WorkspaceProtocol.JobDispatchSnapshotDTO,
  "job_status" | "active_job_id" | "blocked_by_job_id" | "delivery_policy" | "enqueue_sequence"
> & {
  job_status: JobDispatchStatus;
  active_job_id?: string | null;
  blocked_by_job_id?: string | null;
  delivery_policy?: DeliveryPolicy | null;
  enqueue_sequence?: number | null;
};
export type Job = Omit<
  WorkspaceProtocol.JobDTO,
  | "created_at"
  | "updated_at"
  | "metadata"
  | "progress"
  | "mode"
  | "status"
  | "current_step"
  | "error_message"
  | "ended_at"
> & {
  created_at: string;
  updated_at: string;
  metadata: JsonObject;
  progress: number;
  mode: RunMode;
  status: JobStatus;
  current_step?: string | null;
  error_message?: string | null;
  ended_at?: string | null;
};
export type Message = Omit<
  WorkspaceProtocol.MessageDTO,
  "created_at" | "updated_at" | "metadata" | "role" | "attachments"
> & {
  created_at: string;
  updated_at: string;
  metadata: JsonObject;
  role: MessageRole;
  attachments?: AttachmentRef[];
};

export type JobControlRequest = Omit<WorkspaceProtocol.JobControlRequest, "scope" | "action" | "params" | "agent_id" | "step_id" | "message"> & {
  scope?: ControlScope;
  action: ControlAction;
  agent_id?: string | null;
  step_id?: string | null;
  message?: string | null;
  params?: JsonObject;
};
export type JobControlResponse = Omit<WorkspaceProtocol.JobControlResponseDTO, "status"> & {
  status: JobStatus;
};
export type LLMRequestLogRecordDTO = WorkspaceProtocol.LLMRequestLogRecordDTO;
export type MessageCreateRequest = Omit<WorkspaceProtocol.MessageCreateRequest, "role" | "metadata" | "attachments"> & {
  role?: MessageRole;
  attachments?: AttachmentRef[];
  metadata?: JsonObject;
};
export type RunOptions = Omit<WorkspaceProtocol.RunOptions, "mode" | "context" | "delivery_policy"> & {
  mode?: RunMode;
  context?: JsonObject;
  delivery_policy?: DeliveryPolicy;
};
export type MessageRunRequest = Omit<WorkspaceProtocol.MessageRunRequest, "message" | "run"> & {
  message: MessageCreateRequest;
  run: RunOptions;
};
export type MessageRunAccepted = Omit<WorkspaceProtocol.MessageRunAccepted, "status" | "dispatch" | "turn_id"> & {
  status: JobDispatchStatus;
  dispatch: JobDispatchSnapshot;
  turn_id: string | null;
};
export type MessageReplayRequest = Omit<WorkspaceProtocol.MessageReplayRequest, "action" | "content"> & {
  action: "retry_failed" | "regenerate" | "edit_and_continue";
  content?: string | null;
};
export type MessageReplayAccepted = Omit<WorkspaceProtocol.MessageReplayAccepted, "status" | "dispatch" | "action" | "turn_id"> & {
  status: JobDispatchStatus;
  dispatch: JobDispatchSnapshot;
  action: "retry_failed" | "regenerate" | "edit_and_continue";
  turn_id: string | null;
};
export type AgentStateMessages = WorkspaceProtocol.AgentStateMessagesDTO;
export type PendingRequest = Omit<
  WorkspaceProtocol.PendingRequestDTO,
  "attachments" | "delivery_policy" | "status" | "waiting_reason" | "last_boundary" | "message_metadata" | "created_at" | "updated_at" | "turn_id" | "execution_binding"
> & {
  turn_id: string | null;
  execution_binding: JsonObject;
  attachments?: AttachmentRef[];
  delivery_policy: DeliveryPolicy;
  status?: "queued";
  waiting_reason?: string | null;
  last_boundary?: "idle" | DeliveryPolicy | null;
  message_metadata?: JsonObject;
  created_at: string;
  updated_at: string;
};
export type PendingRequestList = Omit<WorkspaceProtocol.PendingRequestListDTO, "active_job_id" | "requests" | "thread_id"> & {
  active_job_id?: string | null;
  thread_id?: string | null;
  requests?: PendingRequest[];
};
export type PendingRequestPolicyUpdateRequest = WorkspaceProtocol.PendingRequestPolicyUpdateRequest;

export type Session = Omit<WorkspaceProtocol.SessionDTO, "created_at" | "updated_at" | "title_source" | "current_provider_id" | "parent_session_id" | "context_source_session_id" | "kind" | "generation_origin"> & {
  created_at: string;
  updated_at: string;
  title_source?: "default" | "user" | "auto";
  current_provider_id?: string | null;
  parent_session_id?: string | null;
  context_source_session_id?: string | null;
  kind?: "normal" | "context_fork";
  generation_origin?: WorkspaceProtocol.SessionGenerationOriginDTO | null;
};
export interface SessionMainThread {
  session_id: string;
  thread_id: string;
}
export type SessionSkillUntrackResult = Omit<WorkspaceProtocol.SessionSkillUntrackResultDTO,
  "mode" | "status" | "append_status" | "display_uri" | "revision" | "content_hash" | "error"
> & {
  mode: "untrack";
  status: "loaded" | "already_active" | "not_tracked" | "rebound" | "error";
  append_status: "appended" | "already_active" | "none";
  display_uri: string | null;
  revision: string | null;
  content_hash: string | null;
  error: WorkspaceProtocol.SessionSkillUntrackErrorDTO | null;
};
export type SessionUpdateRequest = WorkspaceProtocol.SessionUpdateRequest;
export type DeleteSessionResult = WorkspaceProtocol.DeleteSessionResultDTO;
export type SessionCompactResult = Omit<WorkspaceProtocol.SessionCompactResultDTO, "status" | "summary" | "strategy" | "compacted_at"> & {
  status: "scheduled" | "compacted" | "skipped";
  summary?: string | null;
  strategy?: "cache_preserving" | "cache_replacement" | null;
  compacted_at?: string | null;
};
export type SessionInformationSnapshot = Omit<WorkspaceProtocol.SessionInformationSnapshotDTO, "generated_at" | "session" | "workspace" | "execution" | "trace" | "relations" | "resources" | "recent_errors"> & {
  generated_at: string;
  session: Omit<WorkspaceProtocol.SessionInformationSessionDTO, "created_at" | "updated_at" | "current_provider_id" | "parent_session_id" | "context_source_session_id"> & {
    created_at: string;
    updated_at: string;
    current_provider_id?: string | null;
    parent_session_id?: string | null;
    context_source_session_id?: string | null;
  };
  workspace: WorkspaceProtocol.SessionInformationWorkspaceDTO;
  execution: Omit<WorkspaceProtocol.SessionInformationExecutionDTO, "job_id" | "status" | "current_tool" | "last_error"> & {
    job_id?: string | null;
    status?: string;
    current_tool?: string | null;
    last_error?: string | null;
  };
  trace: Omit<WorkspaceProtocol.SessionInformationTraceDTO, "last_event_id" | "last_event_type" | "last_event_at"> & {
    last_event_id?: string | null;
    last_event_type?: string | null;
    last_event_at?: string | null;
  };
  relations: WorkspaceProtocol.SessionInformationRelationsDTO;
  resources: Omit<WorkspaceProtocol.SessionInformationResourceSummaryDTO, "active_count" | "recent_closed_count"> & {
    active_count?: number;
    recent_closed_count?: number;
  };
  recent_errors?: WorkspaceProtocol.SessionInformationErrorDTO[];
};
export type InterruptSessionResult = Omit<WorkspaceProtocol.SessionInterruptResultDTO, "tool_name" | "interrupted_at"> & {
  tool_name?: string | null;
  interrupted_at?: string;
};

export type SessionResource = Omit<WorkspaceProtocol.SessionResourceDTO, "kind" | "created_at" | "updated_at" | "started_at" | "ended_at" | "available_actions" | "metadata"> & {
  kind: "background_task" | "terminal" | "browser";
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  ended_at?: string | null;
  available_actions: ("pause" | "resume" | "cancel" | "delete" | "reconcile")[];
  metadata: JsonObject;
};
export type SessionResourceList = Omit<WorkspaceProtocol.SessionResourceListDTO, "items"> & {
  items: SessionResource[];
};
export type SessionResourceControlResult = Omit<
  WorkspaceProtocol.SessionResourceControlResultDTO,
  "resource"
> & {
  resource?: SessionResource | null;
};

export type SessionChangesSummaryDTO = WorkspaceProtocol.SessionChangesSummaryDTO;
export type SessionChangesetDTO = WorkspaceProtocol.SessionChangesetDTO;
export type SessionChangesetListDTO = WorkspaceProtocol.SessionChangesetListDTO;
export type SessionChangesetListItemDTO = WorkspaceProtocol.SessionChangesetListItemDTO;
export type SessionFileChangeDTO = WorkspaceProtocol.SessionFileChangeDTO;
export type SessionFileReviewResultDTO = WorkspaceProtocol.SessionFileReviewResultDTO;

export type TurnAttachment = Omit<WorkspaceProtocol.TurnAttachmentDTO, "name" | "content_type" | "availability"> & {
  name?: string | null;
  content_type?: string | null;
  availability?: "available" | "unavailable" | null;
};
export type TurnActivityStats = WorkspaceProtocol.TurnActivityStatsDTO;
export type TurnDetailBatch = Omit<WorkspaceProtocol.TurnDetailBatchDTO, "items" | "next_cursor"> & {
  items: TurnDetail[];
  next_cursor?: string | null;
};
export type TurnDetailBatchRequest = Omit<WorkspaceProtocol.TurnDetailBatchRequest, "turn_ids" | "include"> & {
  turn_ids: string[];
  tool_call_ids?: string[] | null;
  include?: string[];
};
export type TurnDetail = Omit<WorkspaceProtocol.TurnDetailDTO, "created_at" | "updated_at" | "completed_at" | "source_message_ids" | "merged_job_ids" | "user_messages" | "assistant_text" | "thinking_blocks" | "tool_summary" | "response_parts" | "items" | "activity_stats"> & {
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  source_message_ids?: string[];
  merged_job_ids?: string[];
  user_messages?: TurnUserMessage[];
  assistant_text?: string[];
  thinking_blocks?: TurnThinkingBlock[];
  tool_summary?: TurnToolSummary[];
  response_parts?: TurnResponsePart[];
  items?: TraceEventDTO[];
  activity_stats?: TurnActivityStats;
};
export type TurnHistoryLoadRequest = Omit<WorkspaceProtocol.TurnHistoryLoadRequest, "turn_ids" | "tool_call_ids" | "include"> & {
  turn_ids?: string[] | null;
  tool_call_ids?: string[] | null;
  include?: string[];
};
export type TurnHistoryPage = Omit<
  WorkspaceProtocol.TurnHistoryPageDTO,
  "items" | "summaries" | "next_cursor" | "before_cursor" | "after_cursor" | "internal_executions"
> & {
  items: TurnDetail[];
  summaries?: TurnSummary[];
  internal_executions?: InternalExecutionHistory[];
  next_cursor?: string | null;
  before_cursor?: string | null;
  after_cursor?: string | null;
};
export type InternalDisplayProvenance = WorkspaceProtocol.InternalDisplayProvenanceDTO;
export type InternalExecutionDisplayPart = Omit<
  WorkspaceProtocol.InternalExecutionDisplayPartDTO,
  "kind" | "created_at" | "provenance"
> & {
  kind: "generated_session_result";
  created_at: string;
  provenance: InternalDisplayProvenance;
};
export type InternalExecutionStatus =
  | "completed"
  | "completed_empty"
  | "failed"
  | "interrupted"
  | "cancelled"
  | "execution_lost"
  | "unknown";
/** execution 的 session_id/thread_id 保留历史 scope，可不同于所在 history page 的请求容器；它们仅用于目标本地 binding 校验，不用于重新寻址来源。 */
export type InternalExecutionHistory = Omit<
  WorkspaceProtocol.InternalExecutionHistoryDTO,
  | "created_at"
  | "updated_at"
  | "display_parts"
  | "response_parts"
  | "status"
  | "logical_item_ordinal"
> & {
  created_at: string;
  updated_at: string;
  status: InternalExecutionStatus;
  display_parts: InternalExecutionDisplayPart[];
  response_parts: TurnResponsePart[];
};
export type TurnJobSummary = WorkspaceProtocol.TurnJobSummaryDTO;
export type TurnPage = Omit<WorkspaceProtocol.TurnPageDTO, "items" | "next_cursor"> & {
  items: TurnSummary[];
  next_cursor?: string | null;
};
export type TurnSummary = Omit<
  WorkspaceProtocol.TurnSummaryDTO,
  "created_at" | "updated_at" | "completed_at" | "source_message_ids" | "merged_job_ids" | "user_messages" | "thinking_blocks" | "tool_summary" | "response_parts" | "activity_stats"
> & {
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  source_message_ids?: string[];
  merged_job_ids?: string[];
  user_messages?: TurnUserMessageSummary[];
  thinking_blocks?: TurnThinkingBlock[];
  tool_summary?: TurnToolSummary[];
  response_parts?: TurnResponsePart[];
  activity_stats?: TurnActivityStats;
};
export type TurnResponseSource = WorkspaceProtocol.TurnResponseSourceDTO;
export type TurnResponsePart = Omit<WorkspaceProtocol.TurnResponsePartDTO, "kind" | "projection" | "status" | "source" | "carrier_type" | "tool_call_id" | "tool_name" | "arguments" | "result"> & {
  kind: "text" | "reasoning" | "reasoning_summary" | "reasoning_encrypted" | "compaction_summary" | "tool_call" | "tool_result" | "final_text";
  projection: "summary" | "detail" | "streaming";
  status?: "pending" | "running" | "completed" | "failed" | "cancelled";
  source: TurnResponseSource;
  carrier_type?: string | null;
  tool_call_id?: string | null;
  tool_name?: string | null;
  arguments?: string | null;
  result?: string | null;
  outcome_unknown?: boolean;
};
export type TurnThinkingBlock = Omit<WorkspaceProtocol.TurnThinkingBlockDTO, "kind"> & {
  kind: "reasoning" | "summary" | "encrypted";
};
export type TurnToolSummary = Omit<WorkspaceProtocol.TurnToolSummaryDTO, "tool_call_id"> & {
  tool_call_id?: string | null;
};
export type TurnUserMessage = Omit<WorkspaceProtocol.TurnUserMessageDTO, "attachments" | "metadata" | "created_at"> & {
  attachments?: TurnAttachment[];
  metadata?: JsonObject;
  created_at: string;
};
export type TurnUserMessageSummary = Omit<WorkspaceProtocol.TurnUserMessageSummaryDTO, "preview" | "created_at"> & {
  preview?: string;
  created_at: string;
};
export type SessionTurnBootstrap = Omit<WorkspaceProtocol.SessionTurnBootstrapDTO, "session" | "latest_turn" | "active_job_id" | "active_jobs" | "projection_state" | "older_cursor" | "event_cursor"> & {
  session: Session;
  latest_turn?: TurnSummary | null;
  active_job_id?: string | null;
  active_jobs?: TurnJobSummary[];
  active_job_count?: number;
  active_jobs_truncated?: boolean;
  projection_state?: "ready" | "partial";
  older_cursor?: string | null;
  event_cursor?: string | null;
};
export type StaleTurnCursorError = WorkspaceProtocol.StaleTurnCursorErrorDTO;
export type StaleTurnReferenceError = WorkspaceProtocol.StaleTurnReferenceErrorDTO;

export type FileTreeShortcut = Omit<WorkspaceProtocol.FileTreeShortcutDTO, "source"> & {
  source: "session" | "workspace";
};
export type FileTreeShortcutRequest = Omit<WorkspaceProtocol.FileTreeShortcutRequest, "label"> & {
  label?: string | null;
};
export type SessionFileTreeSettings = Omit<WorkspaceProtocol.SessionFileTreeSettingsDTO, "session_shortcuts" | "workspace_shortcuts" | "default_shortcuts" | "effective_shortcuts"> & {
  session_shortcuts?: FileTreeShortcut[];
  workspace_shortcuts?: FileTreeShortcut[];
  default_shortcuts?: FileTreeShortcut[];
  effective_shortcuts?: FileTreeShortcut[];
};
export type WorkspaceInfo = Omit<WorkspaceProtocol.WorkspaceDTO, "project_type" | "git" | "runtime"> & {
  project_type?: string | null;
  git?: JsonObject;
  runtime?: JsonObject;
};
export type WorkspaceFileContent = Omit<WorkspaceProtocol.WorkspaceFileContentDTO, "modified_at"> & {
  modified_at?: string | null;
};
export type WorkspaceFileStreamBatch = Omit<WorkspaceProtocol.WorkspaceFileChangeBatchDTO, "directories" | "snapshots"> & {
  directories: WorkspaceDirectoryRevision[];
  snapshots: WorkspaceFileList[];
};
export type WorkspaceFileStreamChange = WorkspaceProtocol.WorkspaceFileChangeDTO;
export type WorkspaceFileList = Omit<WorkspaceProtocol.WorkspaceFileListDTO, "items" | "truncated" | "limit" | "next_cursor" | "directory_key"> & {
  directory_key: WorkspaceProtocol.WorkspaceDirectoryKeyDTO;
  items?: WorkspaceFileNode[];
  truncated?: boolean;
  limit?: number;
  next_cursor?: string | null;
};
export type WorkspaceFileNode = Omit<WorkspaceProtocol.WorkspaceFileNodeDTO, "has_children" | "size" | "modified_at"> & {
  has_children?: boolean;
  size?: number | null;
  modified_at?: string | null;
};
export type WorkspaceFileReveal = WorkspaceProtocol.WorkspaceFileRevealDTO;
export type WorkspaceDirectoryKey = WorkspaceProtocol.WorkspaceDirectoryKeyDTO;
export type WorkspaceDirectoryRevision = Omit<WorkspaceProtocol.WorkspaceDirectoryRevisionDTO, "directory_key"> & { directory_key: WorkspaceDirectoryKey };
export type WorkspaceFileMutationIntent = Omit<WorkspaceProtocol.WorkspaceFileMutationIntentDTO,
  "directory_key" | "expected_directory_revision" | "directory_revision_from_operation_id" | "created_by_operation_id"> & {
  directory_key: WorkspaceDirectoryKey;
  expected_directory_revision?: number | null;
  directory_revision_from_operation_id?: string | null;
  created_by_operation_id?: string | null;
};
export type WorkspaceFileOperationReceipt = Omit<WorkspaceProtocol.WorkspaceFileOperationReceiptDTO, "state" | "directories" | "snapshots"> & {
  state: import("../api/session/sessionCatalogOperations").NavigationMutationState;
  directories: WorkspaceDirectoryRevision[];
  snapshots: WorkspaceFileList[];
};
export type WorkspaceFileOperationStatus = Omit<WorkspaceProtocol.WorkspaceFileOperationStatusDTO, "operations"> & { operations: WorkspaceFileOperationReceipt[] };
export type WorkspaceFileCopyRequest = Omit<WorkspaceProtocol.WorkspaceFileCopyRequest, "intent"> & { intent: WorkspaceFileMutationIntent };
export type WorkspaceFileUpdateRequest = WorkspaceProtocol.WorkspaceFileUpdateRequest;
export type WorkspaceFileCreateRequest = Omit<WorkspaceProtocol.WorkspaceFileCreateRequest, "intent"> & { intent: WorkspaceFileMutationIntent };
export type WorkspaceFilePasteRequest = Omit<WorkspaceProtocol.WorkspaceFilePasteRequest, "intent"> & { intent: WorkspaceFileMutationIntent };
export type WorkspaceFileWatchRequest = WorkspaceProtocol.WorkspaceFileWatchRequest;

// 业务代码仍会使用 DTO 后缀；这些别名只保留命名兼容，实际结构统一来自本文件的协议适配类型。
export type MessageDTO = Message;
export type SessionDTO = Session;
export type SessionInformationSnapshotDTO = SessionInformationSnapshot;
export type SessionResourceDTO = SessionResource;
export type SessionResourceListDTO = SessionResourceList;
export type SessionResourceControlResultDTO = SessionResourceControlResult;
export type TurnSummaryDTO = TurnSummary;
export type TurnDetailDTO = TurnDetail;
export type TurnDetailBatchDTO = TurnDetailBatch;
export type TurnHistoryPageDTO = TurnHistoryPage;
export type TurnPageDTO = TurnPage;
export type WorkspaceFileListDTO = WorkspaceFileList;
export type WorkspaceFileNodeDTO = WorkspaceFileNode;
