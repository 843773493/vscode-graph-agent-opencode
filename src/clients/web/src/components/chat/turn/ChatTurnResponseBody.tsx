import React from "react";
import type { TurnHistoryInclude } from "../../../api/sessionTurnHistory";
import {
  conversationModelUsage,
  conversationTokenUsage,
} from "../../../state/tokenUsage";
import { buildPendingStatusItem, isLiveConversationView } from "../../../state/trace/traceAggregation";
import type { TimelineItem } from "../../../state/timelineTypes";
import {
  responsePartsToTimelineItems,
} from "../../../state/responseParts";
import type { ConversationView } from "../../../types/frontend";
import type { MessageStreamActivity } from "../../../state/messageStream";
import MarkdownContent from "../MarkdownContent";
import ResponseActionToolbar from "../ResponseActionToolbar";
import ThinkingSection from "../ThinkingSection";
import ToolRow from "../ToolRow";
import { activityRendererRegistry } from "./activityRenderers";
import type { ChatTurnActions } from "./useChatTurnActions";

function ErrorPart({ item }: { item: Extract<TimelineItem, { kind: "trace" }> }) {
  const message = [item.payload.error, item.payload.message, item.payload.detail]
    .find((value): value is string => typeof value === "string" && value.trim().length > 0);
  return (
    <div className="chat-inline-error" role="alert">
      <span className="codicon codicon-error" aria-hidden="true" />
      <span>{message ?? "运行失败"}</span>
    </div>
  );
}

function CancelledPart({
  userInitiated,
  label,
}: {
  userInitiated: boolean;
  label?: string;
}) {
  return (
    <div className="chat-inline-cancelled" role="status">
      <span className="codicon codicon-debug-stop" aria-hidden="true" />
      <span>{label ?? (userInitiated ? "已由用户中断" : "任务已取消")}</span>
    </div>
  );
}

function RewindStatusPart({ conversation }: { conversation: ConversationView }) {
  const action = conversation.userMessage?.metadata?.replay_action;
  if (
    action !== "retry_failed"
    && action !== "regenerate"
    && action !== "edit_and_continue"
  ) {
    return null;
  }
  const label = action === "edit_and_continue"
    ? "已回退上下文，从编辑后的消息继续"
    : action === "regenerate"
      ? "已回退上下文，正在重新生成回复"
      : "已回退上下文，正在重试失败轮次";
  return (
    <div className="chat-inline-rewind" role="status" data-status-kind="rewind">
      <span className="codicon codicon-history" aria-hidden="true" />
      <span>{label}</span>
      <span className="chat-working-detail">工作区文件修改不会被撤销</span>
    </div>
  );
}

function activityIcon(status: MessageStreamActivity["status"]): string {
  if (status === "completed") return "codicon-check";
  if (status === "failed") return "codicon-error";
  if (status === "unknown") return "codicon-warning";
  return "codicon-sync codicon-modifier-spin";
}

function activityRole(status: string): "status" | "alert" {
  return status === "failed" || status === "unknown" ? "alert" : "status";
}

function ActivityStatusPart({
  activity,
}: {
  activity: MessageStreamActivity;
}) {
  return (
    <div
      className={`chat-inline-activity is-${activity.status}`}
      role={activityRole(activity.status)}
      data-activity-id={activity.activity_id}
    >
      <span className={`codicon ${activityIcon(activity.status)}`} aria-hidden="true" />
      <span>{activityRendererRegistry.render(activity)}</span>
    </div>
  );
}

function ActivityHistory({
  activities,
  excludeActivityId,
}: {
  activities: MessageStreamActivity[] | undefined;
  excludeActivityId?: string;
}) {
  const visibleActivities = (activities ?? []).filter(
    (activity) => activity.activity_id !== excludeActivityId,
  );
  if (visibleActivities.length === 0) return null;
  return (
    <div className="chat-activity-history" aria-label="消息流 Activity 状态">
      {visibleActivities.map((activity) => (
        <ActivityStatusPart key={activity.activity_id} activity={activity} />
      ))}
    </div>
  );
}

type HistoricalBoundaryStatus = {
  kind: "user-interrupted" | "cancelled" | "tool-incomplete" | "tool-outcome-unknown" | "tool-failed" | "turn-failed" | "turn-timed-out";
  role: "status" | "alert";
  className: string;
  icon: string;
  title: string;
  detail?: string;
};

type ToolTimelineItem = Extract<TimelineItem, { kind: "aggregated_tool" }>;

function uniqueToolNames(items: ToolTimelineItem[]): string[] {
  return items
    .map((item) => item.toolName)
    .filter((toolName, index, names) => names.indexOf(toolName) === index);
}

function historicalBoundaryStatus(
  conversation: ConversationView,
): HistoricalBoundaryStatus | null {
  if (conversation.displayMode !== "history" || conversation.messageStream) return null;
  const responseParts = conversation.responseParts ?? [];
  const userInterrupted = responseParts.some(
    (part) => part.partial === true && part.completion_reason === "user_interrupt",
  );
  const terminalCancellation = conversation.turnStatus === "cancelled";
  const toolItems = responsePartsToTimelineItems(
    responseParts.filter((part) => part.kind === "tool_call"),
    {
      terminalFailure: conversation.turnStatus === "failed"
        || conversation.turnStatus === "timed_out",
      terminalCancellation,
    },
  ).filter((item): item is ToolTimelineItem => item.kind === "aggregated_tool");

  // 用户中断是最高优先级。被中断的工具调用不能再渲染成后端故障或未知结果。
  if (userInterrupted) {
    return {
      kind: "user-interrupted",
      role: "status",
      className: "chat-inline-cancelled",
      icon: "codicon-debug-stop",
      title: "已由用户中断",
      detail: "已保留本轮已经生成的内容",
    };
  }
  if (terminalCancellation) {
    return {
      kind: "cancelled",
      role: "status",
      className: "chat-inline-cancelled",
      icon: "codicon-debug-stop",
      title: "任务已取消",
      detail: "已保留本轮已经生成的内容",
    };
  }

  const incompleteToolNames = uniqueToolNames(toolItems.filter((item) => item.incomplete));
  if (incompleteToolNames.length > 0) {
    return {
      kind: "tool-incomplete",
      role: "status",
      className: "chat-inline-cancelled chat-inline-tool-status",
      icon: "codicon-debug-stop",
      title: "工具调用未完成",
      detail: `${incompleteToolNames.join("、")}：调用在完成前结束`,
    };
  }
  const unknownToolNames = uniqueToolNames(toolItems.filter((item) => item.outcomeUnknown));
  if (unknownToolNames.length > 0) {
    return {
      kind: "tool-outcome-unknown",
      role: "alert",
      className: "chat-inline-error chat-inline-tool-unknown",
      icon: "codicon-warning",
      title: "工具执行结果未知",
      detail: `${unknownToolNames.join("、")}：后端未返回结果，无法确认是否成功`,
    };
  }
  const failedToolNames = uniqueToolNames(toolItems.filter((item) => item.failed));
  if (failedToolNames.length > 0) {
    return {
      kind: "tool-failed",
      role: "alert",
      className: "chat-inline-error chat-inline-tool-status",
      icon: "codicon-error",
      title: "工具执行失败",
      detail: `${failedToolNames.join("、")}：工具返回了失败结果`,
    };
  }
  if (conversation.turnStatus === "failed") {
    return {
      kind: "turn-failed",
      role: "alert",
      className: "chat-inline-error chat-inline-tool-status",
      icon: "codicon-error",
      title: "本轮执行失败",
      detail: "后端没有提供可用的失败详情",
    };
  }
  if (conversation.turnStatus === "timed_out") {
    return {
      kind: "turn-timed-out",
      role: "alert",
      className: "chat-inline-error chat-inline-tool-status",
      icon: "codicon-watch",
      title: "本轮执行超时",
      detail: "任务超过总执行时间上限，已停止执行",
    };
  }
  return null;
}

function HistoricalBoundaryStatusPart({ conversation }: { conversation: ConversationView }) {
  const status = historicalBoundaryStatus(conversation);
  if (!status) return null;
  return (
    <div
      className={status.className}
      role={status.role}
      data-status-kind={status.kind}
    >
      <span className={`codicon ${status.icon}`} aria-hidden="true" />
      <span>{status.title}</span>
      {status.detail ? <span className="chat-working-detail">{status.detail}</span> : null}
    </div>
  );
}

function MessageStreamStatusPart({ conversation }: { conversation: ConversationView }) {
  const stream = conversation.messageStream;
  if (!stream) {
    if (
      conversation.displayMode === "live"
      && (conversation.status === "running" || conversation.status === "queued")
    ) {
      return (
        <div className="chat-working" role="status">
          <span className="codicon codicon-sync codicon-modifier-spin" aria-hidden="true" />
          <span>正在连接实时消息流</span>
        </div>
      );
    }
    return null;
  }
  const activeActivity = stream.activeState?.kind === "activity"
    ? (stream.activities ?? []).find(
      (item) => item.activity_id === stream.activeState?.activity_id,
    )
    : undefined;
  const activityHistory = (
    <ActivityHistory
      activities={stream.activities}
      excludeActivityId={activeActivity?.activity_id}
    />
  );
  if (stream.streamStatus === "interrupting") {
    return (
      <>
        {activityHistory}
        <div className="chat-working" role="status" data-status-kind="interrupting">
          <span className="codicon codicon-debug-stop" aria-hidden="true" />
          <span>正在中断本轮任务</span>
          <span className="chat-working-detail">正在等待模型、工具和 Activity 完成停止确认</span>
        </div>
      </>
    );
  }
  if (stream.streamStatus === "interrupted") {
    return <>{activityHistory}<CancelledPart userInitiated /></>;
  }
  if (stream.streamStatus === "failed" && stream.failure) {
    if (stream.failure.code === "job_timeout" || conversation.turnStatus === "timed_out") {
      return (
        <>
          {activityHistory}
          <div className="chat-inline-error" role="alert">
            <span className="codicon codicon-watch" aria-hidden="true" />
            <span>本轮执行超时</span>
            <span>{stream.failure.message}</span>
          </div>
        </>
      );
    }
    const failureTitle = stream.failure.code === "execution_lost"
      ? "执行丢失"
      : stream.failure.code === "tool_dispatch_timeout"
        ? "工具分派超时"
        : stream.failure.code === "execution_cancelled"
          ? "内部执行取消"
          : "运行失败";
    return (
      <>
        {activityHistory}
        <div className="chat-inline-error" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>{failureTitle}</span>
          <span>{stream.failure.message}</span>
        </div>
      </>
    );
  }
  if (stream.streamStatus === "failed") {
    return (
      <>
        {activityHistory}
        <div className="chat-inline-error" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>消息流失败，但后端没有提供失败详情</span>
        </div>
      </>
    );
  }
  // 终态是后端权威事实；即使旧的连接镜像残留 gap，也不能继续向用户
  // 展示“正在恢复”，否则完整结果会被误报为未完成。
  if (stream.streamStatus === "completed") {
    return activityHistory;
  }
  if (stream.connectionStatus === "disconnected") {
    return (
      <div className="chat-working" role="status">
        <span className="codicon codicon-cloud-offline" aria-hidden="true" />
        <span>实时消息流已断开，正在重连</span>
        <span className="chat-working-detail">已提交的内容仍保留，重连后将从 event_seq {stream.lastEventSeq} 继续</span>
        {stream.protocolError ? (
          <span className="chat-working-detail">诊断：{stream.protocolError}</span>
        ) : null}
      </div>
    );
  }
  if (stream.connectionStatus === "gap") {
    return (
      <div className="chat-inline-error" role="alert">
        <span className="codicon codicon-warning" aria-hidden="true" />
        <span>实时消息流出现缺口，正在请求 snapshot 恢复</span>
        {stream.protocolError ? (
          <span className="chat-working-detail">诊断：{stream.protocolError}</span>
        ) : null}
      </div>
    );
  }
  if (activeActivity) {
    return <>{activityHistory}<ActivityStatusPart activity={activeActivity} /></>;
  }
  if (stream.activeState?.kind === "activity") {
    const activityKind = stream.activeState.activity_kind ?? stream.activeState.entity_id;
    return (
      <>
        {activityHistory}
        <div className="chat-working" role="status" data-status-kind="activity">
          <span className="codicon codicon-sync codicon-modifier-spin" aria-hidden="true" />
          <span>正在处理 Activity</span>
          <span className="chat-working-detail">
            {activityKind ? `${activityKind} 的详细状态暂不可用` : "Activity 的详细状态暂不可用"}
          </span>
        </div>
      </>
    );
  }
  return activityHistory;
}

function needsExecutionRecovery(conversation: ConversationView): boolean {
  if (
    conversation.messageStream?.failure?.code === "execution_lost"
    || conversation.messageStream?.failure?.code === "execution_cancelled"
    || conversation.messageStream?.failure?.code === "tool_dispatch_timeout"
  ) {
    return true;
  }
  return conversation.events.some((event) => {
    if (event.type !== "session_interrupted") return false;
    const payload = event.raw?.payload ?? event.payload ?? {};
    return payload.code === "execution_lost"
      || payload.code === "execution_cancelled"
      || payload.code === "tool_dispatch_timeout"
      || payload.phase === "process_exit";
  });
}

function ExecutionLostRecoveryPart({
  conversation,
  actions,
  running,
}: {
  conversation: ConversationView;
  actions: ChatTurnActions;
  running: boolean;
}): React.ReactNode {
  if (
    running
    || conversation.pending
    || !conversation.userMessage
    || !needsExecutionRecovery(conversation)
    || actions.confirmAction !== null
  ) {
    return null;
  }
  return (
    <div
      className="chat-turn-action-confirmation chat-execution-recovery"
      role="group"
      aria-label="恢复执行丢失的轮次"
      data-status-kind="execution-lost-recovery"
    >
      <div className="chat-turn-action-warning">
        原 AgentLoop 已安全终止，不能续接；已保留已生成内容和工作区修改。
      </div>
      <div className="chat-request-edit-actions">
        <button
          type="button"
          disabled={actions.actionRunning}
          onClick={() => actions.setConfirmAction("retry_failed")}
        >
          重试本轮
        </button>
      </div>
    </div>
  );
}

function ResponsePart({
  item,
  showRawDetails,
  onLoadToolDetails,
}: {
  item: TimelineItem;
  showRawDetails: boolean;
  onLoadToolDetails?: (toolCallId: string) => Promise<void>;
}): React.ReactNode {
  if (item.kind === "aggregated_text" && item.partKind === "markdown") {
    return (
      <MarkdownContent
        value={item.text}
        className={item.active ? "is-streaming" : ""}
        streaming={item.active}
      />
    );
  }
  if (item.kind === "aggregated_tool") {
    return (
      <ToolRow
        item={item}
        showRawDetails={showRawDetails}
        onLoadDetails={onLoadToolDetails}
      />
    );
  }
  if (
    item.kind === "trace"
    && ["job_cancelled", "session_interrupted"].includes(item.eventType)
  ) {
    return <CancelledPart userInitiated={item.eventType === "session_interrupted"} />;
  }
  if (
    item.kind === "trace"
    && ["error", "job_failed"].includes(item.eventType)
  ) {
    return <ErrorPart item={item} />;
  }
  return null;
}

type WorkItem =
  | Extract<TimelineItem, { kind: "aggregated_text" }>
  | Extract<TimelineItem, { kind: "aggregated_tool" }>;

type RenderGroup =
  | { kind: "work"; id: string; items: WorkItem[] }
  | { kind: "response"; id: string; item: TimelineItem };

function persistedWorkItems(conversation: ConversationView): WorkItem[] {
  return responseItemsForConversation(conversation)
    .filter(
      (item): item is WorkItem =>
        item.kind === "aggregated_tool"
        || (item.kind === "aggregated_text" && item.partKind === "reasoning"),
    );
}

function responseItemsForConversation(conversation: ConversationView): TimelineItem[] {
  const terminalCancellation = conversation.turnStatus === "cancelled"
    || conversation.messageStream?.streamStatus === "interrupted";
  const items = responsePartsToTimelineItems(
    (conversation.responseParts ?? []).filter((part) => part.kind !== "final_text"),
    {
      terminalFailure: !terminalCancellation
        && (conversation.turnStatus === "failed"
          || conversation.turnStatus === "timed_out"
          || (!conversation.turnStatus && conversation.status === "error")),
      terminalCancellation,
    },
  );
  const assistantMessages = conversation.assistantMessages ?? [];
  const finalAssistantMessage = assistantMessages.reduce<NonNullable<ConversationView["assistantMessages"]>[number] | undefined>(
    (longest, candidate) => !longest || candidate.content.length > longest.content.length
      ? candidate
      : longest,
    undefined,
  );
  const finalText = finalAssistantMessage?.content?.trim() ?? "";
  if (!finalText) {
    return items;
  }

  const lastMarkdown = [...items].reverse().find(
    (item): item is Extract<TimelineItem, { kind: "aggregated_text" }> =>
      item.kind === "aggregated_text" && item.partKind === "markdown",
  );
  if (lastMarkdown?.text.trim() === finalText) {
    return items;
  }

  // Turn 摘要可能只带 assistantMessages.response_preview，没有 response_parts。
  // 将这个权威最终正文补成同一种 TimelineItem，避免完成后只剩用户消息。
  return [
    ...items,
    {
      kind: "aggregated_text",
      id: `${conversation.conversationId}:assistant-final`,
      text: finalAssistantMessage?.content ?? "",
      partKind: "markdown",
      active: false,
      timestamp: finalAssistantMessage?.created_at ?? null,
      eventCount: 1,
      rawEvents: [],
    },
  ];
}

function buildRenderGroups(items: TimelineItem[]): RenderGroup[] {
  const groups: RenderGroup[] = [];
  for (const item of items) {
    const isWork = item.kind === "aggregated_tool"
      || (item.kind === "aggregated_text"
        && item.partKind === "reasoning");
    if (!isWork) {
      groups.push({ kind: "response", id: item.id, item });
      continue;
    }
    const previous = groups[groups.length - 1];
    if (previous?.kind === "work") previous.items.push(item);
    else groups.push({ kind: "work", id: `work-${item.id}`, items: [item] });
  }
  return groups;
}

function formatActivityDuration(durationMs: number | null | undefined): string {
  if (durationMs === null || durationMs === undefined) return "耗时 —";
  if (durationMs < 1000) return `耗时 ${durationMs}ms`;
  return `耗时 ${(durationMs / 1000).toFixed(durationMs >= 10_000 ? 0 : 1)}s`;
}

function activityStatsPreview(
  stats: ConversationView["activityStats"],
): string {
  if (!stats) return "消息统计不可用";
  const values = [formatActivityDuration(stats.duration_ms)];
  values.push(`消息 ${stats.message_count} 条`);
  return values.join(" · ");
}

function ActivityDetails({
  items,
  showRawDetails,
  onLoadToolDetails,
}: {
  items: WorkItem[];
  showRawDetails: boolean;
  onLoadToolDetails?: (toolCallId: string) => Promise<void>;
}): React.ReactNode {
  if (items.length === 0) {
    return <div className="chat-thinking-empty">没有可展开的中间消息</div>;
  }
  const hasRedactedThinking = items.some(
    (item) => item.kind === "aggregated_text" && item.redacted === true,
  );
  return (
    <>
      {hasRedactedThinking ? (
        <div className="chat-thinking-notice" role="status">
          <span className="codicon codicon-lock" aria-hidden="true" />
          <span>部分思考内容已隐藏</span>
        </div>
      ) : null}
      {items.map((item) => item.kind === "aggregated_tool" ? (
        <ToolRow
          key={item.id}
          item={item}
          showRawDetails={showRawDetails}
          onLoadDetails={onLoadToolDetails}
        />
      ) : (
        <MarkdownContent key={item.id} value={item.text} />
      ))}
    </>
  );
}

function TurnActivitySummary({
  conversation,
  items,
  showRawDetails,
  onLoadTurnDetails,
  onLoadToolDetails,
}: {
  conversation: ConversationView;
  items: WorkItem[];
  showRawDetails: boolean;
  onLoadTurnDetails?: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
    include?: TurnHistoryInclude[],
    toolCallIds?: string[],
  ) => Promise<void>;
  onLoadToolDetails?: (turnId: string, toolCallId: string) => Promise<void>;
}): React.ReactNode {
  const [open, setOpen] = React.useState(false);
  const [loading, setLoading] = React.useState(false);
  const [loaded, setLoaded] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const turnId = conversation.turnId;
  const boundaryStatus = historicalBoundaryStatus(conversation);
  const activityPreview = activityStatsPreview(conversation.activityStats);
  const toggleLabel = boundaryStatus?.title;

  const toggle = async () => {
    if (loading) return;
    if (open) {
      setOpen(false);
      return;
    }
    setError(null);
    if (!loaded && turnId && onLoadTurnDetails) {
      setLoading(true);
      try {
        await onLoadTurnDetails(
          [turnId],
          `turn-activity:${turnId}`,
          false,
          [
            "user",
            "text",
            "reasoning_detail",
            "encrypted_reasoning_meta",
            "tool_summary",
            "final_response",
          ],
        );
        setLoaded(true);
      } catch (loadError) {
        setError(loadError instanceof Error ? loadError.message : String(loadError));
      } finally {
        setLoading(false);
      }
    }
    setOpen(true);
  };

  return (
    <section
      className={`chat-thinking chat-turn-activity ${open ? "is-open" : "is-complete"}${boundaryStatus ? " has-boundary" : ""}`}
      data-status-kind={boundaryStatus?.kind}
    >
      <button
        type="button"
        className="chat-thinking-toggle"
        aria-expanded={open}
        aria-label={open ? "收起 Turn 中间消息" : `展开 Turn 中间消息${toggleLabel ? `（${toggleLabel}）` : ""}`}
        onClick={() => void toggle()}
      >
        <span
          className={`codicon ${boundaryStatus?.icon ?? "codicon-check"}`}
          aria-hidden="true"
        />
        <span className="chat-thinking-preview">
          {loading
            ? "正在加载中间消息…"
            : boundaryStatus
              ? `${activityPreview} · ${boundaryStatus.title}`
              : activityPreview}
        </span>
        {boundaryStatus?.detail ? (
          <span className="chat-working-detail">{boundaryStatus.detail}</span>
        ) : null}
        <span
          className={`codicon ${open ? "codicon-chevron-down" : "codicon-chevron-right"}`}
          aria-hidden="true"
        />
      </button>
      {open ? (
        <div className="chat-thinking-body">
          {error ? (
            <div className="chat-inline-error" role="alert">
              <span className="codicon codicon-error" aria-hidden="true" />
              <span>{error}</span>
            </div>
          ) : (
            <ActivityDetails
              items={items}
              showRawDetails={showRawDetails}
              onLoadToolDetails={
                turnId && onLoadToolDetails
                  ? (toolCallId) => onLoadToolDetails(turnId, toolCallId)
                  : undefined
              }
            />
          )}
        </div>
      ) : null}
    </section>
  );
}

export default function ChatTurnResponseBody({
  conversation,
  showRawDetails,
  actions,
  onLoadTurnDetails,
  onLoadToolDetails,
}: {
  conversation: ConversationView;
  showRawDetails: boolean;
  actions: ChatTurnActions;
  onLoadTurnDetails?: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
    include?: TurnHistoryInclude[],
    toolCallIds?: string[],
  ) => Promise<void>;
  onLoadToolDetails?: (turnId: string, toolCallId: string) => Promise<void>;
}): React.ReactNode {
  const running = isLiveConversationView(conversation)
    && (conversation.status === "running" || conversation.status === "queued");
  const summaryOnly = conversation.turnItemsView === "summary";
  const hasPersistedResponse = (conversation.responseParts?.length ?? 0) > 0
    || (conversation.assistantMessages?.length ?? 0) > 0;
  const hasUnifiedParts = !running && hasPersistedResponse;
  // 实时回答只来自 message.v1；旧 Trace 仍可在事件/请求视图查看，
  // 但不能在聊天主线作为静默兼容回退，避免两套语义互相覆盖。
  const parts = hasUnifiedParts || Boolean(conversation.messageStream)
    ? responseItemsForConversation(conversation)
    : [];
  const hasSessionInterrupted = parts.some((item) =>
    item.kind === "trace" && item.eventType === "session_interrupted"
  );
  const visibleParts = parts.filter((item) =>
    item.kind === "aggregated_text"
    || item.kind === "aggregated_tool"
    || (item.kind === "trace"
      && ["error", "job_failed", "job_cancelled", "session_interrupted"]
        .includes(item.eventType)
      && !(hasSessionInterrupted && item.eventType === "job_cancelled")),
  );
  const renderGroups = buildRenderGroups(visibleParts);
  const persistedWork = persistedWorkItems(conversation);
  const historyTurn = conversation.displayMode === "history" && Boolean(conversation.turnId);
  const activityItems = historyTurn
    ? persistedWork
    : [
      ...persistedWork,
      ...renderGroups
        .filter((group): group is Extract<RenderGroup, { kind: "work" }> => group.kind === "work")
        .flatMap((group) => group.items),
    ];
  const responseTextPart = [...visibleParts].reverse().find(
    (item): item is Extract<TimelineItem, { kind: "aggregated_text" }> =>
      item.kind === "aggregated_text" && item.partKind === "markdown",
  );
  const hasActiveWork = visibleParts.some((item) =>
    (item.kind === "aggregated_tool"
      || (item.kind === "aggregated_text" && item.partKind === "reasoning"))
    && item.active,
  );
  const hasStreamingMarkdown = visibleParts.some((item) =>
    item.kind === "aggregated_text" && item.partKind === "markdown" && item.active
  );
  const status = running
    && Boolean(conversation.messageStream)
    && !hasActiveWork
    && !hasStreamingMarkdown
    ? buildPendingStatusItem(conversation)
    : null;
  const showResponseActions = !running && !conversation.pending;
  return (
    <>
      <RewindStatusPart conversation={conversation} />
      {historyTurn ? (
        <TurnActivitySummary
          conversation={conversation}
          items={activityItems}
          showRawDetails={showRawDetails}
          onLoadTurnDetails={onLoadTurnDetails}
          onLoadToolDetails={onLoadToolDetails}
        />
      ) : null}
      {renderGroups.map((group) => group.kind === "work" ? (
        historyTurn ? null : (
          <ThinkingSection
            key={group.id}
            items={group.items}
            active={running && group.items.some((item) => item.active)}
            showRawDetails={showRawDetails}
          />
        )
      ) : (
        <ResponsePart
          key={group.id}
          item={group.item}
          showRawDetails={showRawDetails}
          onLoadToolDetails={
            historyTurn && onLoadToolDetails && conversation.turnId
              ? (toolCallId) => onLoadToolDetails(conversation.turnId!, toolCallId)
              : undefined
          }
        />
      ))}
      {summaryOnly && !historyTurn ? (
        <div className="chat-turn-detail-loading" role="status">
          <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />
          <span>正在加载完整内容…</span>
        </div>
      ) : null}
      {status ? (
        <div className="chat-working" role="status">
          <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />
          <span>{status.title}</span>
          <span className="chat-working-detail">{status.detail}</span>
        </div>
      ) : null}
      <MessageStreamStatusPart conversation={conversation} />
      {!historyTurn ? <HistoricalBoundaryStatusPart conversation={conversation} /> : null}
      <ExecutionLostRecoveryPart
        conversation={conversation}
        actions={actions}
        running={running}
      />
      {showResponseActions ? (
        <ResponseActionToolbar
          responseText={responseTextPart?.text ?? ""}
          tokenUsage={conversationTokenUsage(conversation)}
          modelUsage={conversationModelUsage(conversation)}
        />
      ) : null}
      {actions.confirmAction ? (
        <div className="chat-turn-action-confirmation" role="group" aria-label="确认轮次操作">
          <div className="chat-turn-action-warning">
            将移除此消息之后的会话上下文，但不会撤销已产生的文件修改。
          </div>
          <div className="chat-request-edit-actions">
            <button
              type="button"
              disabled={actions.actionRunning}
              onClick={() => actions.setConfirmAction(null)}
            >
              取消
            </button>
            <button
              type="button"
              className="primary"
              disabled={actions.actionRunning}
              onClick={() => void actions.executeReplay(actions.confirmAction!)}
            >
              {actions.actionRunning
                ? "正在执行..."
                : actions.confirmAction === "regenerate"
                  ? "确认重新生成"
                  : "确认重试"}
            </button>
          </div>
        </div>
      ) : null}
      {actions.actionError ? (
        <div className="chat-inline-error" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>{actions.actionError}</span>
        </div>
      ) : null}
    </>
  );
}
