import {
  lazy,
  Suspense,
  useEffect,
  useState,
  type ComponentProps,
  type ReactNode,
} from "react";
import BootstrapState from "./BootstrapState";
import ChatPanel from "../panels/ChatPanel";
import { FRONTEND_EVENT_QUEUE_LIMIT } from "../../state/traceEvents";
import type { SessionTurnTimeline } from "../../state/session/turnTimeline";
import type {
  GatewayUserViewState,
  LLMRequestLogRecord,
  Session,
  SessionChangeset,
  SessionChangesSummary,
} from "../../types/backend";
import type {
  ConversationContentView,
  ConversationView,
  FrontendReceivedEvent,
  SessionTraceHistoryState,
} from "../../types/frontend";

const AgentStatePanel = lazy(() => import("../panels/inspectionPanels/AgentStatePanel"));
const EventQueuePanel = lazy(() => import("../panels/inspectionPanels/EventQueuePanel"));
const RequestLogPanel = lazy(() => import("../panels/inspectionPanels/RequestLogPanel"));

/**
 * 主窗口会话区的四个内容视图槽：上下文状态、事件、请求与会话记录。
 *
 * 四个槽共用「保留外壳 + 隐藏」编排；检视面板首次打开时才挂载，之后隐藏保留，
 * 避免首屏初始化未访问的面板。启动失败与启动骨架也由本组件在应用外壳层处理。
 */

type ChatPanelProps = ComponentProps<typeof ChatPanel>;

interface ContentViewSlotsProps extends Pick<
  ChatPanelProps,
  | "onLoadOlderMessages"
  | "onLoadNewerMessages"
  | "onLoadAroundTurn"
  | "onLoadTurnDetails"
  | "onLoadToolDetails"
  | "onLoadAgentStateMessageRawContent"
  | "onRetryHistory"
  | "onOpenChanges"
  | "onReplayTurn"
  | "onRemovePending"
  | "onChangePendingPolicy"
  | "onOpenAttachment"
  | "onForkConversation"
  | "onOpenChildReport"
  | "onViewStateChange"
  | "onViewStateRestoreStatus"
> {
  error: string | null;
  isBootstrapping: boolean;
  onRetryGatewayState: () => Promise<void>;
  contentView: ConversationContentView;
  apiPort: number;
  workspaceId: string | null;
  session: Session | null;
  sessionId: string | null;
  hasActiveSession: boolean;
  activeSessionCacheKey: string | null;
  expandedDetails: boolean;
  agentStateJsonl: string;
  agentStateMessageCount: number;
  agentStateLoadedAt: string | null;
  agentStateLoading: boolean;
  agentStateError: string | null;
  receivedEvents: FrontendReceivedEvent[];
  activeTraceHistory: SessionTraceHistoryState | null;
  onLoadOlderTraceHistory: () => Promise<number>;
  onRetryTraceHistory: () => Promise<void>;
  requestLogs: LLMRequestLogRecord[];
  requestLogsLoading: boolean;
  requestLogsError: string | null;
  requestLogsLoadedAt: string | null;
  onRetryRequestLogs: () => void;
  conversations: ConversationView[];
  activeTurnTimeline: SessionTurnTimeline | null;
  changesHint: { sessionId: string; summary: SessionChangesSummary } | null;
  changesHintLoading: boolean;
  activeChangeset: SessionChangeset | null;
  gatewayUserViewStates: Map<string, GatewayUserViewState>;
}

function ContentViewSlot({
  visible,
  mountAfterFirstVisit = false,
  children,
}: {
  visible: boolean;
  mountAfterFirstVisit?: boolean;
  children: ReactNode;
}) {
  const [hasBeenVisible, setHasBeenVisible] = useState(visible);
  const shouldRenderChildren = !mountAfterFirstVisit || hasBeenVisible || visible;
  const slotChildren = mountAfterFirstVisit ? (
    <Suspense
      fallback={
        <div className="empty-state" role="status">
          正在加载面板...
        </div>
      }
    >
      {children}
    </Suspense>
  ) : children;

  useEffect(() => {
    if (mountAfterFirstVisit && visible && !hasBeenVisible) {
      setHasBeenVisible(true);
    }
  }, [hasBeenVisible, mountAfterFirstVisit, visible]);

  return (
    <div
      className={`content-view-slot${visible ? "" : " preserve-mounted-hidden"}`}
      hidden={!visible}
    >
      {shouldRenderChildren ? slotChildren : null}
    </div>
  );
}

export default function ContentViewSlots({
  error,
  isBootstrapping,
  onRetryGatewayState,
  contentView,
  apiPort,
  workspaceId,
  session,
  sessionId,
  hasActiveSession,
  activeSessionCacheKey,
  expandedDetails,
  agentStateJsonl,
  agentStateMessageCount,
  agentStateLoadedAt,
  agentStateLoading,
  agentStateError,
  receivedEvents,
  activeTraceHistory,
  onLoadOlderTraceHistory,
  onRetryTraceHistory,
  requestLogs,
  requestLogsLoading,
  requestLogsError,
  requestLogsLoadedAt,
  onRetryRequestLogs,
  conversations,
  activeTurnTimeline,
  changesHint,
  changesHintLoading,
  activeChangeset,
  gatewayUserViewStates,
  onLoadOlderMessages,
  onLoadNewerMessages,
  onLoadAroundTurn,
  onLoadTurnDetails,
  onLoadToolDetails,
  onLoadAgentStateMessageRawContent,
  onRetryHistory,
  onOpenChanges,
  onReplayTurn,
  onRemovePending,
  onChangePendingPolicy,
  onOpenAttachment,
  onForkConversation,
  onOpenChildReport,
  onViewStateChange,
  onViewStateRestoreStatus,
}: ContentViewSlotsProps) {
  if (error) {
    return (
      <div className="empty-state error-state">
        <div className="error-title">前端初始化失败</div>
        <div className="error-message">{error}</div>
        <button
          type="button"
          className="error-retry-button"
          onClick={() => void onRetryGatewayState().catch(() => undefined)}
        >
          重新加载工作区
        </button>
      </div>
    );
  }

  if (isBootstrapping) {
    return <BootstrapState onRetry={onRetryGatewayState} />;
  }

  const conversationVisible = ![
    "agent",
    "events",
    "requests",
  ].includes(contentView);
  const activeSessionChangeHint =
    changesHint && changesHint.sessionId === sessionId
      ? changesHint.summary
      : contentView === "changes" && activeChangeset
        ? activeChangeset.summary
        : null;

  return (
    <>
      <ContentViewSlot visible={contentView === "agent"} mountAfterFirstVisit>
        <AgentStatePanel
          port={apiPort}
          workspaceId={workspaceId ?? ""}
          session={session}
          active={contentView === "agent"}
          jsonl={agentStateJsonl}
          messageCount={agentStateMessageCount}
          loadedAt={agentStateLoadedAt}
          loading={agentStateLoading}
          error={agentStateError}
        />
      </ContentViewSlot>
      <ContentViewSlot visible={contentView === "events"} mountAfterFirstVisit>
        <EventQueuePanel
          items={receivedEvents}
          limit={FRONTEND_EVENT_QUEUE_LIMIT}
          sessionId={sessionId ?? ""}
          scopeKey={activeSessionCacheKey ?? ""}
          active={contentView === "events"}
          historyLoading={activeTraceHistory?.loading ?? false}
          historyLoadingOlder={activeTraceHistory?.loadingOlder ?? false}
          historyHasMore={activeTraceHistory?.hasMore ?? false}
          historyError={activeTraceHistory?.error ?? null}
          onLoadOlderHistory={onLoadOlderTraceHistory}
          onRetryHistory={() => void onRetryTraceHistory()}
        />
      </ContentViewSlot>
      <ContentViewSlot visible={contentView === "requests"} mountAfterFirstVisit>
        <RequestLogPanel
          logs={requestLogs}
          loading={requestLogsLoading}
          error={requestLogsError}
          loadedAt={requestLogsLoadedAt}
          sessionId={sessionId ?? ""}
          scopeKey={activeSessionCacheKey ?? ""}
          active={contentView === "requests"}
          onRetryRequestLogs={onRetryRequestLogs}
        />
      </ContentViewSlot>
      <ContentViewSlot visible={conversationVisible}>
        <ChatPanel
          apiPort={apiPort}
          workspaceId={workspaceId}
          sessionId={sessionId}
          threadId={activeTurnTimeline?.threadId ?? null}
          conversations={conversations}
          internalExecutions={activeTurnTimeline?.internalExecutions ?? []}
          expandDetails={expandedDetails}
          hasActiveSession={hasActiveSession}
          hasOlderMessages={activeTurnTimeline?.hasBefore ?? false}
          loadingOlderMessages={activeTurnTimeline?.loadingBefore ?? false}
          hasNewerMessages={activeTurnTimeline?.hasAfter ?? false}
          loadingNewerMessages={activeTurnTimeline?.loadingAfter ?? false}
          historyLoading={hasActiveSession && (
            !activeTurnTimeline || activeTurnTimeline.phase === "bootstrapping"
          )}
          projectionState={activeTurnTimeline?.projectionState ?? "ready"}
          historyError={activeTurnTimeline?.error ?? null}
          onLoadOlderMessages={onLoadOlderMessages}
          onLoadNewerMessages={onLoadNewerMessages}
          onLoadAroundTurn={onLoadAroundTurn}
          onLoadTurnDetails={onLoadTurnDetails}
          onLoadToolDetails={onLoadToolDetails}
          onLoadAgentStateMessageRawContent={onLoadAgentStateMessageRawContent}
          onRetryHistory={onRetryHistory}
          sessionChangeSummary={activeSessionChangeHint}
          sessionChangesLoading={changesHintLoading}
          onOpenChanges={onOpenChanges}
          onReplayTurn={onReplayTurn}
          onRemovePending={onRemovePending}
          onChangePendingPolicy={onChangePendingPolicy}
          onOpenAttachment={onOpenAttachment}
          onForkConversation={onForkConversation}
          onOpenChildReport={onOpenChildReport}
          viewState={
            activeSessionCacheKey
              ? gatewayUserViewStates.get(activeSessionCacheKey) ?? null
              : null
          }
          onViewStateChange={onViewStateChange}
          onViewStateRestoreStatus={onViewStateRestoreStatus}
        />
      </ContentViewSlot>
    </>
  );
}
