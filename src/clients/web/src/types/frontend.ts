// 前端内部类型
import type {
  Agent,
  GatewayWorkspace,
  LLMRequestLogRecord,
  Message,
  Session,
  SessionChangeset,
  SessionChangesetListItem,
  SessionCompactResult,
  SessionGoal,
  SessionResource,
  TraceEvent,
  TurnResponsePart,
  WebUiSettings,
  DeliveryPolicy,
  GatewayUserAccess,
  GatewayUserViewState,
} from "./backend";
import type { SessionTurnTimeline } from "../state/session/turnTimeline";

export type ConversationContentView =
  | "default"
  | "events"
  | "requests"
  | "changes"
  | "resources"
  | "agent";

export type CreatableSessionConnectionKind = "terminal" | "browser";

export type FrontendEventSource =
  | "frontend"
  | "initial_load"
  | "recovery"
  | "sse";

interface FrontendReceivedEventBase {
  id: string;
  sessionId: string;
  receivedAt: string;
  source: FrontendEventSource;
}

export interface FrontendReceivedTraceEvent extends FrontendReceivedEventBase {
  kind: "trace";
  event: TraceEvent;
}

export interface FrontendReceivedLifecycleEvent
  extends FrontendReceivedEventBase {
  kind: "frontend";
  type:
    | "session_selected"
    | "session_created"
    | "session_context_forked"
    | "session_renamed"
    | "agent_switched"
    | "model_switched"
    | "context_compacted"
    | "session_load_started"
    | "session_load_completed"
    | "session_load_failed";
  title: string;
  detail?: string;
  payload?: Record<string, unknown>;
}

export type FrontendReceivedEvent =
  | FrontendReceivedTraceEvent
  | FrontendReceivedLifecycleEvent;

export interface ConversationView {
  conversationId: string;
  /**
   * history 只允许使用 Turn projection；live 只允许使用 pending/SSE 状态。
   * 活动 Turn 终止后必须先移除 live 视图，再由历史 projection 建立 history 视图。
   */
  displayMode: "history" | "live";
  /** 展示历史中的权威 Turn 身份；待处理消息和旧诊断路径没有该字段。 */
  turnId?: string;
  turnRevision?: number;
  turnItemsView?: "summary" | "full";
  activityStats?: {
    duration_ms: number | null;
    message_count: number;
  };
  sessionId: string;
  userMessage: Message | null;
  /** 当历史 trace 不完整时，用持久化 Assistant 消息恢复最终正文。 */
  assistantMessages?: Message[];
  thinkingBlocks?: Array<{
    kind: "reasoning" | "summary" | "encrypted";
    text: string;
  }>;
  toolSummary?: Array<{
    tool_name: string;
    status: string;
    tool_call_id?: string | null;
  }>;
  responseParts?: TurnResponsePart[];
  // 助手消息内容由 ChatPanel 从 traceEvents 聚合得到，不再在 hooks 中维护。
  events: TraceEvent[];
  status: "queued" | "running" | "done" | "error";
  jobId: string | null;
  pending: boolean;
  pendingSubmissionId?: string;
  activeJobOverlay?: boolean;
  deliveryPolicy?: DeliveryPolicy;
  enqueueSequence?: number;
  waitingReason?: string | null;
  queueSnapshotVersion?: number;
  pendingPosition?: number;
  source: "turn" | "pending";
}

export interface ConversationTokenUsage {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cacheReadInputTokens: number | null;
  modelCalls: number;
  reportedModelCalls: number;
}

export interface ConversationModelUsage {
  finalModelId: string;
}

export interface SessionAttachmentSummary {
  count: number;
  names: string[];
  latestAt: string | null;
}

export interface AppState {
  apiPort: number | null;
  gatewayWorkspaces: GatewayWorkspace[];
  activeGatewayWorkspaceId: string | null;
  sessionsByWorkspace: Map<string, Session[]>;
  sessionGatewayWorkspaceById: Map<string, string>;
  removingGatewayWorkspaceIds: Set<string>;
  sessionHistoryReloadNonce: number;
  workspaceSwitching: boolean;
  gatewayError: string | null;
  gatewayUserAccess: GatewayUserAccess | null;
  gatewayUserViewStates: Map<string, GatewayUserViewState>;
  uiSettings: WebUiSettings;
  uiSettingsLoaded: boolean;
  workspaceRoot: string | null;
  workspaceName: string | null;
  agents: Agent[];
  sessions: Session[];
  sessionAttachmentSummaries: Map<string, SessionAttachmentSummary>;
  currentSession: Session | null;
  currentSessionWorkspaceId: string | null;
  turnTimelinesBySession: Map<string, SessionTurnTimeline>;
  traceEvents: TraceEvent[];
  llmRequestLogs: LLMRequestLogRecord[];
  llmRequestLogsLoadedAt: string | null;
  llmRequestLogsLoading: boolean;
  llmRequestLogsError: string | null;
  sessionChangesets: SessionChangesetListItem[];
  selectedChangesetId: string | null;
  activeChangeset: SessionChangeset | null;
  sessionChangesLoadedAt: string | null;
  sessionChangesLoading: boolean;
  sessionChangesError: string | null;
  sessionResources: SessionResource[];
  sessionResourcesLoadedAt: string | null;
  sessionResourcesLoading: boolean;
  sessionResourcesError: string | null;
  eventQueuesBySession: Map<string, FrontendReceivedEvent[]>;
  sessionTraceHistoryBySession: Map<string, SessionTraceHistoryState>;
  pendingConversations: Map<string, ConversationView[]>;
  activeJobIdsBySession: Map<string, string>;
  unreadSessionKeys: Set<string>;
  status: string;
  error: string | null;
  isBootstrapping: boolean;
  expandDetails: boolean;
  agentSessionsPanelOpen: boolean;
  contentView: ConversationContentView;
  agentStateJsonl: string;
  agentStateMessageCount: number;
  agentStateLoadedAt: string | null;
  agentStateLoading: boolean;
  agentStateError: string | null;
  compactLoading: boolean;
  lastCompactResult: SessionCompactResult | null;
  currentGoal: SessionGoal | null;
  currentGoalSessionId: string | null;
  goalLoading: boolean;
  goalError: string | null;
}

export interface SessionTraceHistoryState {
  scopeKey: string;
  generation: number;
  items: TraceEvent[];
  nextCursor: string | null;
  hasMore: boolean;
  loading: boolean;
  loadingOlder: boolean;
  error: string | null;
}
