import React from "react";
import { conversationTokenUsage } from "../../state/tokenUsage";
import { aggregateConversationEvents, buildPendingStatusItem } from "../../state/trace/traceAggregation";
import type { TimelineItem } from "../../state/timelineTypes";
import type { ConversationView } from "../../types/frontend";
import AttachmentList from "../AttachmentList";
import MarkdownContent from "./MarkdownContent";
import ResponseActionToolbar from "./ResponseActionToolbar";
import ThinkingSection from "./ThinkingSection";
import ToolRow from "./ToolRow";

function assistantFallback(conversation: ConversationView): string {
  const messages = conversation.assistantMessages ?? [];
  const candidates = messages
    .map((message) => ({
      content: message.content.trim(),
      phase: message.metadata?.phase,
    }))
    .filter((candidate) => candidate.content.length > 0);
  const isFragmented = (content: string) => {
    const lines = content.split("\n").map((line) => line.trim()).filter(Boolean);
    if (lines.length >= 5 && content.length / lines.length < 6) {
      return true;
    }
    if (lines.length < 10) {
      return false;
    }
    const tinyLines = lines.filter((line) => line.length <= 2).length;
    return tinyLines / lines.length >= 0.35;
  };
  const healthyFinal = candidates.filter(
    (candidate) =>
      candidate.phase === "final_answer" && !isFragmented(candidate.content),
  );
  if (healthyFinal.length > 0) {
    return healthyFinal[healthyFinal.length - 1].content;
  }
  const healthy = candidates.filter((candidate) => !isFragmented(candidate.content));
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
      />
    );
  }
  if (item.kind === "aggregated_tool") {
    return <ToolRow item={item} showRawDetails={showRawDetails} />;
  }
  if (
    item.kind === "trace" &&
    ["error", "job_failed", "job_cancelled", "session_interrupted"].includes(item.eventType)
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
    const isWork =
      item.kind === "aggregated_tool" ||
      (item.kind === "aggregated_text" &&
        (item.partKind === "reasoning" || index !== finalMarkdownIndex));
    if (!isWork) {
      groups.push({ kind: "response", id: item.id, item });
      continue;
    }
    const previous = groups[groups.length - 1];
    if (previous?.kind === "work") {
      previous.items.push(item);
    } else {
      groups.push({ kind: "work", id: `work-${item.id}`, items: [item] });
    }
  }
  return groups;
}

export default function ChatTurn({
  conversation,
  showRawDetails,
}: {
  conversation: ConversationView;
  showRawDetails: boolean;
}): React.ReactNode {
  const parts = React.useMemo(
    () => aggregateConversationEvents(
      conversation.events,
      conversation.conversationId,
      conversation.status === "running" || conversation.status === "queued",
    ),
    [conversation],
  );
  const running = conversation.status === "running" || conversation.status === "queued";
  const persistedResponse = assistantFallback(conversation);
  const preferPersistedResponse = !running && Boolean(persistedResponse);
  const visibleParts = parts.filter((item) =>
    (item.kind === "aggregated_text" &&
      (!preferPersistedResponse || item.partKind !== "markdown")) ||
    item.kind === "aggregated_tool" ||
    (item.kind === "trace" &&
      ["error", "job_failed", "job_cancelled", "session_interrupted"].includes(item.eventType)),
  );
  const renderGroups = buildRenderGroups(visibleParts);
  const finalTextPart = [...visibleParts].reverse().find(
    (item): item is Extract<TimelineItem, { kind: "aggregated_text" }> =>
      item.kind === "aggregated_text" && item.partKind === "markdown",
  );
  const hasFinalText = Boolean(finalTextPart);
  const fallback = preferPersistedResponse
    ? persistedResponse
    : hasFinalText
      ? ""
      : persistedResponse;
  const finalResponseText = finalTextPart?.text ?? fallback;
  const hasActiveWork = visibleParts.some(
    (item) =>
      (item.kind === "aggregated_tool" ||
        (item.kind === "aggregated_text" && item.partKind === "reasoning")) &&
      item.active,
  );
  const hasStreamingMarkdown = visibleParts.some(
    (item) => item.kind === "aggregated_text" && item.partKind === "markdown" && item.active,
  );
  const status = running && !hasActiveWork && !hasStreamingMarkdown
    ? buildPendingStatusItem(conversation)
    : null;
  const showResponseActions = !running && (hasFinalText || Boolean(fallback));
  const tokenUsage = React.useMemo(
    () => conversationTokenUsage(conversation),
    [conversation],
  );
  const userMessage = conversation.userMessage;
  const userAttachments = userMessage?.attachments ?? [];

  return (
    <article className="chat-turn" data-conversation-id={conversation.conversationId}>
      {userMessage ? (
        <div className="chat-user-row">
          <div className="chat-user-bubble">
            {userMessage.content ? <div className="chat-user-text">{userMessage.content}</div> : null}
            {userAttachments.length > 0 ? (
              <AttachmentList attachments={userAttachments} />
            ) : null}
          </div>
        </div>
      ) : null}

      <div className="chat-assistant-row">
        <div className="chat-assistant-avatar" aria-hidden="true">
          <span className="codicon codicon-copilot" />
        </div>
        <div className="chat-assistant-content">
          {renderGroups.map((group) =>
            group.kind === "work" ? (
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
            ),
          )}
          {fallback ? <MarkdownContent value={fallback} /> : null}
          {status ? (
            <div className="chat-working" role="status">
              <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />
              <span>{status.title}</span>
              <span className="chat-working-detail">{status.detail}</span>
            </div>
          ) : null}
          {showResponseActions ? (
            <ResponseActionToolbar
              responseText={finalResponseText}
              tokenUsage={tokenUsage}
            />
          ) : null}
        </div>
      </div>
    </article>
  );
}
