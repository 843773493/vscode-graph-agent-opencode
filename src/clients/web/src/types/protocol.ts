// Web 端公开业务协议的 TypeScript 适配类型。
// 结构和字段名来自 .proto 生成绑定；这里只处理 JSON API 的字符串枚举、可空时间和动态对象。
import type * as WorkspaceProtocol from "./protocol_generated/boxteam/workspace/v2/public";
import type { AttachmentRef as GeneratedAttachmentRef } from "./protocol_generated/boxteam/workspace/v2/message";
import type { TraceEventDTO } from "../protocol/jsonTypes";

export type JsonObject = Record<string, unknown>;
export type DeliveryPolicy = "after_turn" | "after_tool_result" | "after_interrupt";

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

export type AttachmentRef = Omit<GeneratedAttachmentRef, "name" | "content_type" | "data_url"> & {
  name?: string | null;
  content_type?: string | null;
  data_url?: string | null;
};
export type Agent = Omit<WorkspaceProtocol.AgentDTO, "description"> & {
  description?: string | null;
};
export type NodeDebugActionRecord = Omit<
  WorkspaceProtocol.NodeDebugActionRecordDTO,
  "actor" | "tool_name" | "tool_call_id" | "result" | "created_at"
> & {
  actor?: "human" | "ai" | "system";
  tool_name?: string | null;
  tool_call_id?: string | null;
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
export type NodeDebugEvaluation = WorkspaceProtocol.NodeDebugEvaluationDTO;
export type NodeDebugLaunchProfile = WorkspaceProtocol.NodeDebugLaunchProfileDTO;
export type NodeDebugStackFrame = WorkspaceProtocol.NodeDebugStackFrameDTO;
export type NodeDebugStartRequest = WorkspaceProtocol.NodeDebugStartRequest;
export type NodeDebugState = Omit<WorkspaceProtocol.NodeDebugStateDTO, "status" | "active_configuration_id" | "active_configuration_name" | "script_path" | "working_directory" | "launch_profile_name" | "pid" | "paused_reason" | "error_message" | "call_stack" | "last_stopped_frame" | "breakpoints" | "last_evaluation" | "evaluations" | "actions" | "source_changed_paths"> & {
  status: "idle" | "starting" | "running" | "paused" | "exited" | "failed";
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
export type JobDispatchStatus = "queued" | "running";
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
export type MessageRunAccepted = Omit<WorkspaceProtocol.MessageRunAccepted, "status" | "dispatch"> & {
  status: JobDispatchStatus;
  dispatch: JobDispatchSnapshot;
};
export type MessageReplayRequest = Omit<WorkspaceProtocol.MessageReplayRequest, "action" | "content"> & {
  action: "retry_failed" | "regenerate" | "edit_and_continue";
  content?: string | null;
};
export type MessageReplayAccepted = Omit<WorkspaceProtocol.MessageReplayAccepted, "status" | "dispatch" | "action"> & {
  status: JobDispatchStatus;
  dispatch: JobDispatchSnapshot;
  action: "retry_failed" | "regenerate" | "edit_and_continue";
};
export type AgentStateMessages = WorkspaceProtocol.AgentStateMessagesDTO;
export type PendingRequest = Omit<
  WorkspaceProtocol.PendingRequestDTO,
  "attachments" | "delivery_policy" | "status" | "waiting_reason" | "last_boundary" | "message_metadata" | "created_at" | "updated_at"
> & {
  attachments?: AttachmentRef[];
  delivery_policy: DeliveryPolicy;
  status?: "queued";
  waiting_reason?: string | null;
  last_boundary?: "idle" | DeliveryPolicy | null;
  message_metadata?: JsonObject;
  created_at: string;
  updated_at: string;
};
export type PendingRequestList = Omit<WorkspaceProtocol.PendingRequestListDTO, "active_job_id" | "requests"> & {
  active_job_id?: string | null;
  requests?: PendingRequest[];
};
export type PendingRequestPolicyUpdateRequest = WorkspaceProtocol.PendingRequestPolicyUpdateRequest;
export type PendingRequestUpdateRequest = Omit<WorkspaceProtocol.PendingRequestUpdateRequest, "attachments"> & {
  attachments?: AttachmentRef[];
};

export type Session = Omit<WorkspaceProtocol.SessionDTO, "created_at" | "updated_at" | "title_source" | "current_provider_id" | "parent_session_id" | "context_source_session_id" | "kind" | "delegation" | "generation_origin"> & {
  created_at: string;
  updated_at: string;
  title_source?: "default" | "user" | "auto";
  current_provider_id?: string | null;
  parent_session_id?: string | null;
  context_source_session_id?: string | null;
  kind?: "normal" | "context_fork" | "delegated";
  delegation?: WorkspaceProtocol.SessionDelegationDTO | null;
  generation_origin?: WorkspaceProtocol.SessionGenerationOriginDTO | null;
};
export type SessionUpdateRequest = WorkspaceProtocol.SessionUpdateRequest;
export type DeleteSessionResult = WorkspaceProtocol.DeleteSessionResultDTO;
export type SessionCompactResult = Omit<WorkspaceProtocol.SessionCompactResultDTO, "status" | "summary" | "history_file_path" | "strategy" | "compacted_at"> & {
  status: "scheduled" | "compacted" | "skipped";
  summary?: string | null;
  history_file_path?: string | null;
  strategy?: "cache_preserving" | "cache_replacement" | null;
  compacted_at?: string | null;
};
export type SessionInformationSnapshot = Omit<WorkspaceProtocol.SessionInformationSnapshotDTO, "generated_at" | "session" | "workspace" | "execution" | "trace" | "resources" | "recent_errors"> & {
  generated_at: string;
  session: Session;
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
  resources?: WorkspaceProtocol.SessionInformationResourceDTO[];
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
  available_actions: ("pause" | "resume" | "cancel" | "delete")[];
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

export type TurnAttachment = Omit<WorkspaceProtocol.TurnAttachmentDTO, "name" | "content_type"> & {
  name?: string | null;
  content_type?: string | null;
};
export type TurnActivityStats = WorkspaceProtocol.TurnActivityStatsDTO;
export type TurnDetailBatch = Omit<WorkspaceProtocol.TurnDetailBatchDTO, "items" | "next_cursor"> & {
  items: TurnDetail[];
  next_cursor?: string | null;
};
export type TurnDetailBatchRequest = Omit<WorkspaceProtocol.TurnDetailBatchRequest, "turn_ids" | "include"> & {
  turn_ids: string[];
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
export type TurnHistoryLoadRequest = Omit<WorkspaceProtocol.TurnHistoryLoadRequest, "turn_ids" | "include"> & {
  turn_ids?: string[] | null;
  include?: string[];
};
export type TurnHistoryPage = Omit<WorkspaceProtocol.TurnHistoryPageDTO, "items" | "next_cursor" | "before_cursor" | "after_cursor"> & {
  items: TurnDetail[];
  next_cursor?: string | null;
  before_cursor?: string | null;
  after_cursor?: string | null;
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
  kind: "text" | "reasoning" | "reasoning_summary" | "reasoning_encrypted" | "tool_call" | "tool_result" | "final_text";
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
export type WorkspaceFileStreamBatch = WorkspaceProtocol.WorkspaceFileChangeBatchDTO;
export type WorkspaceFileStreamChange = WorkspaceProtocol.WorkspaceFileChangeDTO;
export type WorkspaceFileList = Omit<WorkspaceProtocol.WorkspaceFileListDTO, "items" | "truncated" | "limit" | "next_cursor"> & {
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
export type WorkspaceFileCopyRequest = WorkspaceProtocol.WorkspaceFileCopyRequest;
export type WorkspaceFileUpdateRequest = WorkspaceProtocol.WorkspaceFileUpdateRequest;
export type WorkspaceFileCreateRequest = WorkspaceProtocol.WorkspaceFileCreateRequest;
export type WorkspaceFilePasteRequest = WorkspaceProtocol.WorkspaceFilePasteRequest;
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
