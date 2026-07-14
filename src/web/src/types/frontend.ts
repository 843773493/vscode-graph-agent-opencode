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
  SessionResource,
  TraceEvent,
  WebUiSettings,
} from "./backend";

export type ConversationContentView =
  | "default"
  | "events"
  | "requests"
  | "changes"
  | "resources"
  | "agent";

export type FrontendEventSource =
  | "frontend"
  | "initial_load"
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
  sessionId: string;
  userMessage: Message | null;
  /** 当历史 trace 不完整时，用持久化 Assistant 消息恢复最终正文。 */
  assistantMessages?: Message[];
  // 助手消息内容由 ChatPanel 从 traceEvents 聚合得到，不再在 hooks 中维护。
  events: TraceEvent[];
  status: "queued" | "running" | "done" | "error";
  jobId: string | null;
  pending: boolean;
  pendingSubmissionId?: string;
  source: "messages" | "pending";
}

export interface ConversationTokenUsage {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cacheReadInputTokens: number | null;
  modelCalls: number;
  reportedModelCalls: number;
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
  uiSettings: WebUiSettings;
  uiSettingsLoaded: boolean;
  workspaceRoot: string | null;
  workspaceName: string | null;
  agents: Agent[];
  sessions: Session[];
  sessionAttachmentSummaries: Map<string, SessionAttachmentSummary>;
  currentSession: Session | null;
  currentSessionWorkspaceId: string | null;
  messages: Message[];
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
  pendingConversations: Map<string, ConversationView[]>;
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
}
