import React from "react";
import {
  conversationModelUsage,
  conversationTokenUsage,
} from "../../../state/tokenUsage";
import {
  aggregateConversationEvents,
  buildPendingStatusItem,
} from "../../../state/trace/traceAggregation";
import type { TimelineItem } from "../../../state/timelineTypes";
import type { ConversationView } from "../../../types/frontend";
import MarkdownContent from "../MarkdownContent";
import ResponseActionToolbar from "../ResponseActionToolbar";
import ThinkingSection from "../ThinkingSection";
import ToolRow from "../ToolRow";
import type { ChatTurnActions } from "./useChatTurnActions";

export const LEGACY_FRAGMENTATION_MAX_LENGTH = 64_000;

export function assistantFallback(conversation: ConversationView): string {
  const messages = conversation.assistantMessages ?? [];
  const projectedFinal = messages.find((message) =>
    message.metadata?.source === "turn_projection"
    && message.metadata?.summary !== true
  );
  if (projectedFinal) return projectedFinal.content;

  const candidates: Array<{ content: string; phase: unknown }> = [];
  for (const message of messages) {
    if (message.content.length > LEGACY_FRAGMENTATION_MAX_LENGTH) {
      candidates.push({ content: message.content, phase: message.metadata?.phase });
      continue;
    }
    const content = message.content.trim();
    if (content) candidates.push({ content, phase: message.metadata?.phase });
  }
  const isFragmented = (content: string) => {
    if (content.length > LEGACY_FRAGMENTATION_MAX_LENGTH) return false;
    const lines = content.split("\n").map((line) => line.trim()).filter(Boolean);
    if (lines.length >= 5 && content.length / lines.length < 6) return true;
    if (lines.length < 10) return false;
    return lines.filter((line) => line.length <= 2).length / lines.length >= 0.35;
  };
  const healthyFinal = candidates.filter(
    (candidate) => candidate.phase === "final_answer" && !isFragmented(candidate.content),
  );
  const healthy = healthyFinal.length > 0
    ? healthyFinal
    : candidates.filter((candidate) => !isFragmented(candidate.content));
  return healthy.reduce(
    (best, candidate) => candidate.content.length > best.length ? candidate.content : best,
    "",
  );
}

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

function ResponsePart({
  item,
  showRawDetails,
}: {
  item: TimelineItem;
  showRawDetails: boolean;
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
    return <ToolRow item={item} showRawDetails={showRawDetails} />;
  }
  if (
    item.kind === "trace"
    && ["error", "job_failed", "job_cancelled", "session_interrupted"]
      .includes(item.eventType)
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

function buildRenderGroups(items: TimelineItem[]): RenderGroup[] {
  const groups: RenderGroup[] = [];
  let finalMarkdownIndex = -1;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind === "aggregated_text" && item.partKind === "markdown") {
      finalMarkdownIndex = index;
      break;
    }
  }
  for (const [index, item] of items.entries()) {
    const isWork = item.kind === "aggregated_tool"
      || (item.kind === "aggregated_text"
        && (item.partKind === "reasoning" || index !== finalMarkdownIndex));
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

export default function ChatTurnResponseBody({
  conversation,
  showRawDetails,
  isLastTurn,
  sessionBusy,
  actions,
}: {
  conversation: ConversationView;
  showRawDetails: boolean;
  isLastTurn: boolean;
  sessionBusy: boolean;
  actions: ChatTurnActions;
}): React.ReactNode {
  const running = conversation.status === "running" || conversation.status === "queued";
  const summaryOnly = conversation.turnItemsView === "summary";
  const persistedResponse = assistantFallback(conversation);
  const preferPersistedResponse = !running && Boolean(persistedResponse);
  const parts = aggregateConversationEvents(
    conversation.events,
    conversation.conversationId,
    running,
  );
  const visibleParts = parts.filter((item) =>
    (item.kind === "aggregated_text"
      && (!preferPersistedResponse || item.partKind !== "markdown"))
    || item.kind === "aggregated_tool"
    || (item.kind === "trace"
      && ["error", "job_failed", "job_cancelled", "session_interrupted"]
        .includes(item.eventType)),
  );
  const renderGroups = buildRenderGroups(visibleParts);
  const finalTextPart = [...visibleParts].reverse().find(
    (item): item is Extract<TimelineItem, { kind: "aggregated_text" }> =>
      item.kind === "aggregated_text" && item.partKind === "markdown",
  );
  const fallback = preferPersistedResponse
    ? persistedResponse
    : finalTextPart
      ? ""
      : persistedResponse;
  const hasActiveWork = visibleParts.some((item) =>
    (item.kind === "aggregated_tool"
      || (item.kind === "aggregated_text" && item.partKind === "reasoning"))
    && item.active,
  );
  const hasStreamingMarkdown = visibleParts.some((item) =>
    item.kind === "aggregated_text" && item.partKind === "markdown" && item.active
  );
  const status = running && !hasActiveWork && !hasStreamingMarkdown
    ? buildPendingStatusItem(conversation)
    : null;
  const showResponseActions = !summaryOnly
    && !running
    && (Boolean(finalTextPart) || Boolean(fallback));
  const failedByJob = isLastTurn
    && !summaryOnly
    && !actions.isInternalDisplayMessage
    && conversation.status === "error"
    && !conversation.events.some((event) =>
      ["job_cancelled", "session_interrupted"].includes(event.type)
    );

  return (
    <>
      {renderGroups.map((group) => group.kind === "work" ? (
        <ThinkingSection
          key={group.id}
          items={group.items}
          active={running && group.items.some((item) => item.active)}
          showRawDetails={showRawDetails}
        />
      ) : (
        <ResponsePart
          key={group.id}
          item={group.item}
          showRawDetails={showRawDetails}
        />
      ))}
      {fallback ? (
        <MarkdownContent
          value={fallback}
          renderMode={summaryOnly ? "plain" : "progressive"}
        />
      ) : null}
      {summaryOnly ? (
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
      {showResponseActions ? (
        <ResponseActionToolbar
          responseText={finalTextPart?.text ?? fallback}
          tokenUsage={conversationTokenUsage(conversation)}
          modelUsage={conversationModelUsage(conversation)}
          canRegenerate={isLastTurn && !sessionBusy && !actions.isInternalDisplayMessage}
          onRegenerate={() => actions.setConfirmAction("regenerate")}
        />
      ) : null}
      {failedByJob ? (
        // TODO: 后续为失败轮次重试补齐重试策略、模型切换和参数选择；当前按原输入重试。
        <button
          type="button"
          className="chat-failed-retry-button"
          disabled={actions.actionRunning || sessionBusy}
          onClick={() => actions.setConfirmAction("retry_failed")}
        >
          <span className="codicon codicon-refresh" aria-hidden="true" />
          重试失败轮次
        </button>
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
