import type { ComponentProps, ReactNode } from "react";
import BootstrapState from "./BootstrapState";
import AgentStatePanel from "../panels/inspectionPanels/AgentStatePanel";
import EventQueuePanel from "../panels/inspectionPanels/EventQueuePanel";
import RequestLogPanel from "../panels/inspectionPanels/RequestLogPanel";
import ChatPanel from "../panels/ChatPanel";
import { FRONTEND_EVENT_QUEUE_LIMIT } from "../../state/traceEvents";
import type { SessionTurnTimeline } from "../../state/session/turnTimeline";
import type {
  GatewayUserViewState,
  LLMRequestLogRecord,
  SessionChangeset,
  SessionChangesSummary,
} from "../../types/backend";
import type {
  ConversationContentView,
  ConversationView,
  FrontendReceivedEvent,
  SessionTraceHistoryState,
} from "../../types/frontend";

/**
 * 主窗口会话区的四个内容视图槽：上下文状态、事件、请求与会话记录。
 *
 * 四个槽共用同一套「保留挂载 + 隐藏」外壳，因此收敛到 ContentViewSlot；
 * 启动失败与启动骨架属于外壳级视图编排，与本组件一起留在应用外壳层。
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
  | "onUpdatePending"
  | "onRemovePending"
  | "onChangePendingPolicy"
  | "onOpenAttachment"
  | "onViewStateChange"
  | "onViewStateRestoreStatus"
> {
  error: string | null;
  isBootstrapping: boolean;
  onRetryGatewayState: () => Promise<void>;
  contentView: ConversationContentView;
  apiPort: number;
  workspaceId: string | null;
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
  children,
}: {
  visible: boolean;
  children: ReactNode;
}) {
  return (
    <div
      className={`content-view-slot${visible ? "" : " preserve-mounted-hidden"}`}
      hidden={!visible}
    >
      {children}
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
  onUpdatePending,
  onRemovePending,
  onChangePendingPolicy,
  onOpenAttachment,
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
      <ContentViewSlot visible={contentView === "agent"}>
        <AgentStatePanel
          port={apiPort}
          workspaceId={workspaceId ?? ""}
          sessionId={sessionId ?? ""}
          active={contentView === "agent"}
          jsonl={agentStateJsonl}
          messageCount={agentStateMessageCount}
          loadedAt={agentStateLoadedAt}
          loading={agentStateLoading}
          error={agentStateError}
        />
      </ContentViewSlot>
      <ContentViewSlot visible={contentView === "events"}>
        <EventQueuePanel
          items={receivedEvents}
          limit={FRONTEND_EVENT_QUEUE_LIMIT}
          sessionId={sessionId ?? ""}
          active={contentView === "events"}
          historyLoading={activeTraceHistory?.loading ?? false}
          historyLoadingOlder={activeTraceHistory?.loadingOlder ?? false}
          historyHasMore={activeTraceHistory?.hasMore ?? false}
          historyError={activeTraceHistory?.error ?? null}
          onLoadOlderHistory={onLoadOlderTraceHistory}
          onRetryHistory={() => void onRetryTraceHistory()}
        />
      </ContentViewSlot>
      <ContentViewSlot visible={contentView === "requests"}>
        <RequestLogPanel
          logs={requestLogs}
          loading={requestLogsLoading}
          error={requestLogsError}
          loadedAt={requestLogsLoadedAt}
          sessionId={sessionId ?? ""}
          active={contentView === "requests"}
          onRetryRequestLogs={onRetryRequestLogs}
        />
      </ContentViewSlot>
      <ContentViewSlot visible={conversationVisible}>
        <ChatPanel
          apiPort={apiPort}
          workspaceId={workspaceId}
          conversations={conversations}
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
          onUpdatePending={onUpdatePending}
          onRemovePending={onRemovePending}
          onChangePendingPolicy={onChangePendingPolicy}
          onOpenAttachment={onOpenAttachment}
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
