import React from "react";
import type { ConversationView } from "../../../types/frontend";
import type { MessageStreamActivity } from "../../../state/messageStream/index";
import type { TimelineItem } from "../../../state/timeline/timelineTypes";
import { historicalBoundaryStatus } from "../../../state/timeline/chatResponseGroups";
import { activityRendererRegistry } from "./activityRenderers";
import MarkdownContent from "../MarkdownContent";
import type { ChatTurnActions } from "./useChatTurnActions";

export function ErrorPart({ item }: { item: Extract<TimelineItem, { kind: "trace" }> }) {
  const message = [item.payload.error, item.payload.message, item.payload.detail]
    .find((value): value is string => typeof value === "string" && value.trim().length > 0);
  return (
    <div className="chat-inline-error" role="alert">
      <span className="codicon codicon-error" aria-hidden="true" />
      <span>{message ?? "运行失败"}</span>
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
      <span>{label ?? (userInitiated ? "已由用户中断" : "任务已取消")}</span>
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

function ProtocolErrorDetail({ message }: { message?: string | null }): React.ReactNode {
  return message ? <span className="chat-working-detail">诊断：{message}</span> : null;
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
  if (stream.connectionStatus === "retry_exhausted") {
    return (
      <div className="chat-inline-error" role="alert" data-status-kind="retry-exhausted">
        <span className="codicon codicon-cloud-offline" aria-hidden="true" />
        <span>实时消息流连续重连失败，已停止自动重连；请重新发送消息或刷新页面后重试</span>
        <ProtocolErrorDetail message={stream.protocolError} />
      </div>
    );
  }
  if (stream.connectionStatus === "disconnected") {
    return (
      <div className="chat-working" role="status">
        <span className="codicon codicon-cloud-offline" aria-hidden="true" />
        <span>实时消息流已断开，正在重连</span>
        <span className="chat-working-detail">已提交的内容仍保留，重连后将从 event_seq {stream.lastEventSeq} 继续</span>
        <ProtocolErrorDetail message={stream.protocolError} />
      </div>
    );
  }
  if (stream.connectionStatus === "gap") {
    return (
      <div className="chat-inline-error" role="alert">
        <span className="codicon codicon-warning" aria-hidden="true" />
        <span>实时消息流出现缺口，正在请求 snapshot 恢复</span>
        <ProtocolErrorDetail message={stream.protocolError} />
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

export function ResponsePart({
  item,
}: {
  item: TimelineItem;
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
