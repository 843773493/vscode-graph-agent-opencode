import { t } from "../../../../../shared/core/i18n/index.js";
import React from "react";
import type { ConversationView } from "../../../types/frontend";
import type { MessageStreamActivity } from "../../../state/messageStream/index";
import type { TimelineItem } from "../../../state/timeline/timelineTypes";
import { historicalBoundaryStatus } from "../../../state/timeline/chatResponseGroups";
import { activityRendererRegistry } from "./activityRenderers";
import MarkdownContent from "../MarkdownContent";
import ContextNotice from "../ContextNotice";
import { responseItemMenuActions, responseItemTarget, useChatItemMenu } from "../itemContextMenu";
import type { ChatTurnActions } from "./useChatTurnActions";

export function ErrorPart({ item }: { item: Extract<TimelineItem, { kind: "trace" }> }) {
  const message = [item.payload.error, item.payload.message, item.payload.detail]
    .find((value): value is string => typeof value === "string" && value.trim().length > 0);
  return (
    <div className="chat-inline-error" role="alert">
      <span className="codicon codicon-error" aria-hidden="true" />
      <span>{message ?? t("chat.status.failed")}</span>
    </div>
  );
}

export function CancelledPart({
  userInitiated,
  label,
}: {
  userInitiated: boolean;
  label?: string;
}) {
  return (
    <div className="chat-inline-cancelled" role="status">
      <span className="codicon codicon-debug-stop" aria-hidden="true" />
      <span>{label ?? (userInitiated ? t("chat.status.interrupted") : t("chat.status.cancelled"))}</span>
    </div>
  );
}

export function RewindStatusPart({ conversation }: { conversation: ConversationView }) {
  const action = conversation.userMessage?.metadata?.replay_action;
  if (
    action !== "retry_failed"
    && action !== "regenerate"
    && action !== "edit_and_continue"
  ) {
    return null;
  }
  const label = action === "edit_and_continue"
    ? t("chat.status.editRewind")
    : action === "regenerate"
      ? t("chat.status.regenerateRewind")
      : t("chat.status.retryRewind");
  return (
    <div className="chat-inline-rewind" role="status" data-status-kind="rewind">
      <span className="codicon codicon-history" aria-hidden="true" />
      <span>{label}</span>
      <span className="chat-working-detail">{t("chat.status.filesKept")}</span>
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

export function ActivityStatusPart({
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

export function ActivityHistory({
  activities,
}: {
  activities: MessageStreamActivity[] | undefined;
}) {
  const visibleActivities = (activities ?? []).filter(
    (activity) => ["completed", "failed", "unknown"].includes(activity.status),
  );
  if (visibleActivities.length === 0) return null;
  return (
    <div className="chat-activity-history" aria-label={t("chat.status.activity")}>
      {visibleActivities.map((activity) => (
        <ActivityStatusPart key={activity.activity_id} activity={activity} />
      ))}
    </div>
  );
}

export function HistoricalBoundaryStatusPart({ conversation }: { conversation: ConversationView }) {
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

export function MessageStreamStatusPart({ conversation }: { conversation: ConversationView }) {
  const stream = conversation.messageStream;
  if (!stream) return null;
  const activityHistory = <ActivityHistory activities={stream.activities} />;
  if (stream.streamStatus === "interrupted") {
    return historicalBoundaryStatus(conversation)
      ? activityHistory
      : <>{activityHistory}<CancelledPart userInitiated /></>;
  }
  if (stream.streamStatus === "failed" && stream.failure) {
    if (stream.failure.code === "job_timeout" || conversation.turnStatus === "timed_out") {
      return (
        <>
          {activityHistory}
          <div className="chat-inline-error" role="alert">
            <span className="codicon codicon-watch" aria-hidden="true" />
            <span>{t("chat.status.timedOut")}</span>
            <span>{stream.failure.message}</span>
          </div>
        </>
      );
    }
    const failureTitle = stream.failure.code === "execution_lost"
      ? t("chat.status.lost")
      : stream.failure.code === "tool_dispatch_timeout"
        ? t("chat.status.dispatchTimeout")
        : stream.failure.code === "execution_cancelled"
          ? t("chat.status.internalCancel")
          : t("chat.status.failed");
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
          <span>{t("chat.status.noFailureDetail")}</span>
        </div>
      </>
    );
  }
  // 实时连接及执行阶段统一由输入区状态栏展示，正文只保留本轮的终态结果。
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

export function ExecutionLostRecoveryPart({
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
      aria-label={t("chat.status.recoverLost")}
      data-status-kind="execution-lost-recovery"
    >
      <div className="chat-turn-action-warning">
        {t("chat.status.stoppedLoop")}
      </div>
      <div className="chat-request-edit-actions">
        <button
          type="button"
          disabled={actions.actionRunning}
          onClick={() => actions.setConfirmAction("retry_failed")}
        >
          {t("chat.status.retryTurn")}
        </button>
      </div>
    </div>
  );
}

export function ResponsePart({
  item,
  onForkItem,
}: {
  item: TimelineItem;
  onForkItem?: (itemId: string) => void;
}): React.ReactNode {
  const menu = useChatItemMenu(() => responseItemMenuActions(item, onForkItem), true, () => responseItemTarget(item));
  if (item.kind === "context_event") return <ContextNotice notices={item.notices} />;
  if (item.kind === "aggregated_text" && item.partKind === "markdown") {
    return (
      <MarkdownContent
        value={item.text}
        className={item.active ? "is-streaming" : ""}
        streaming={item.active}
        responseSources={item.responseSources ? JSON.stringify(item.responseSources) : undefined}
        contextMenu={{ ...menu, "aria-label": t("chat.itemMenu.response") }}
      />
    );
  }
  if (item.kind === "aggregated_text" && item.partKind === "compaction") {
    return (
      <details className="chat-compaction-summary" data-response-sources={item.responseSources ? JSON.stringify(item.responseSources) : undefined} {...menu}>
        <summary>{t("chat.status.compactionSummary")}</summary>
        {/* 摘要包含模型协议标签，按原文展示以免被 Markdown 当作 HTML 隐去正文。 */}
        <MarkdownContent value={item.text} renderMode="plain" />
      </details>
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
