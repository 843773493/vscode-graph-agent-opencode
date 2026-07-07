import React from "react";
import {
  buildTraceTimelineItems,
  normalizeTraceData,
  type TimelineItem,
} from "../state/chatTimeline";
import type { ConversationView } from "../types/frontend";
import { escapeHtml, formatDateTime } from "../utils/format";
import { normalizeDisplayText } from "../utils/displayText";
import {
  formatToolCardContent,
  toolCollapsedText,
} from "../state/toolDisplay";
import EventCard from "./EventCard";
import SkillSummaryCard from "./SkillSummaryCard";

function displayTime(value: unknown): string {
  return formatDateTime(value) || "now";
}

// === 子卡片组件 ===

function StatusCard({
  item,
  index,
  showRawDetails,
}: {
  item: Extract<TimelineItem, { kind: "status" }>;
  index: number;
  showRawDetails: boolean;
}): React.ReactNode {
  return (
    <EventCard
      title={item.title}
      kind="system"
      tone="running"
      time={displayTime(item.timestamp)}
      summary={item.detail}
      content={item.detail}
      raw={{ status: true, title: item.title, detail: item.detail }}
      index={index}
      showRawDetails={showRawDetails}
    />
  );
}

function AggregatedTextCard({
  item,
  index,
  showRawDetails,
}: {
  item: Extract<TimelineItem, { kind: "aggregated_text" }>;
  index: number;
  showRawDetails: boolean;
}): React.ReactNode {
  const text = normalizeDisplayText(item.text.trim());
  const isReasoning = item.phase === "reasoning";
  const title = isReasoning ? "推理过程" : item.active ? "回复生成中" : "最终回复";
  const kind = isReasoning ? "thought" : "response";
  const tone = item.active || isReasoning ? "running" : "done";
  const phaseLabel = isReasoning ? "推理" : "回复";
  const summary =
    isReasoning || item.active || item.eventCount > 1
      ? `${item.eventCount} 个${phaseLabel}事件已合并`
      : "";
  const collapsedContent = isReasoning
    ? "推理过程已折叠"
    : item.active
      ? "回复生成中"
      : "最终回复已折叠";
  return (
    <EventCard
      title={title}
      kind={kind}
      tone={tone}
      time={displayTime(item.timestamp)}
      summary={summary}
      content={text || "（空）"}
      collapsedContent={collapsedContent}
      raw={{
        aggregated: true,
        eventCount: item.eventCount,
        phase: item.phase,
        active: item.active,
        text,
        rawEvents: item.rawEvents,
      }}
      index={index}
      defaultOpen={!item.active && !isReasoning}
      showRawDetails={showRawDetails}
    />
  );
}

function AggregatedToolCard({
  item,
  index,
  showRawDetails,
}: {
  item: Extract<TimelineItem, { kind: "aggregated_tool" }>;
  index: number;
  showRawDetails: boolean;
}): React.ReactNode {
  const { toolName, inputText, resultText, timestamp } = item;
  const structuredContent = formatToolCardContent(item);
  const fallbackContent =
    [
      inputText ? `**输入参数**\n\`\`\`\n${inputText}\n\`\`\`` : "",
      resultText ? `**执行结果**\n\`\`\`\n${resultText}\n\`\`\`` : "",
    ]
      .filter(Boolean)
      .join("\n\n") || "（无详情）";
  const content = structuredContent ?? fallbackContent;

  return (
    <EventCard
      title={`🔧 ${toolName}`}
      kind="tool_call"
      tone={item.failed ? "danger" : "done"}
      time={displayTime(timestamp)}
      summary={toolName}
      content={content}
      collapsedContent={toolCollapsedText(item)}
      raw={{
        toolName,
        inputText,
        resultText,
        rawStart: item.rawStart,
        rawEnd: item.rawEnd,
        failed: item.failed,
      }}
      index={index}
      showRawDetails={showRawDetails}
    />
  );
}

function ConversationMarker({
  item,
}: {
  item: Extract<TimelineItem, { kind: "conversation_marker" }>;
}): React.ReactNode {
  return (
    <div
      className="conversation-marker"
      data-job-id={item.jobId ?? undefined}
    >
      <span className="conversation-marker-line" />
      <span className="conversation-marker-label">
        {escapeHtml(item.label)}
      </span>
      <span className="conversation-marker-line" />
    </div>
  );
}

// === 时间线卡片路由 ===

function TimelineCard({
  item,
  index,
  showRawDetails,
}: {
  item: TimelineItem;
  index: number;
  showRawDetails: boolean;
}): React.ReactNode {
  if (item.kind === "status") {
    return (
      <StatusCard
        item={item}
        index={index}
        showRawDetails={showRawDetails}
      />
    );
  }

  if (item.kind === "conversation_marker") {
    return <ConversationMarker item={item} />;
  }

  if (item.kind === "aggregated_text") {
    return <AggregatedTextCard item={item} index={index} showRawDetails={showRawDetails} />;
  }

  if (item.kind === "aggregated_tool") {
    return <AggregatedToolCard item={item} index={index} showRawDetails={showRawDetails} />;
  }

  if (item.kind === "skill_summary") {
    return (
      <SkillSummaryCard
        item={item}
        index={index}
        displayTime={displayTime}
        renderCard={(options) => <EventCard {...options} showRawDetails={showRawDetails} />}
      />
    );
  }

  if (item.kind === "message") {
    const isUser = item.role === "user";
    // Assistant 消息：如果 content 为空且 metadata 中有 tool_calls，说明是工具调用消息，跳过显示
    // 这类消息的实际内容会由 trace 事件中的 tool_call_start/end 展示
    if (!isUser) {
      const trimmed = (item.content || "").trim();
      const hasToolCalls =
        item.metadata?.tool_calls &&
        Array.isArray(item.metadata.tool_calls) &&
        item.metadata.tool_calls.length > 0;
      if (!trimmed && hasToolCalls) {
        return null;
      }
    }
    // Assistant 消息的 content 通常由后端将 reasoning + text 串联合并保存，
    // 单独显示会与上方 aggregated_text（推理过程/最终回复）重复。
    // 这里只展示 content 的长度与简短摘要，详细文本已在独立的思考/回复卡片中呈现。
    const trimmed = (item.content || "").trim();
    const summaryPreview =
      trimmed.length > 80 ? `${trimmed.slice(0, 80)}…` : trimmed;
    const userSummary =
      trimmed ||
      (item.attachments.length > 0 ? `${item.attachments.length} 个附件` : "");

    return (
      <EventCard
        title={isUser ? "用户消息" : "Assistant 消息"}
        kind={isUser ? "system" : "response"}
        tone={isUser ? "running" : "done"}
        time={displayTime(item.createdAt)}
        summary={isUser ? userSummary : summaryPreview || "（无内容）"}
        content={isUser ? "" : trimmed}
        attachments={item.attachments}
        raw={{
          kind: item.kind,
          id: item.id,
          role: item.role,
          content: item.content,
          attachments: item.attachments,
          createdAt: item.createdAt,
          metadata: item.metadata,
        }}
        index={index}
        defaultOpen={false}
        showRawDetails={showRawDetails}
      />
    );
  }

  const payload = item.payload;
  const normalized = normalizeTraceData(item.eventType, payload);
  const tone =
    normalized.kind === "error"
      ? "danger"
      : normalized.kind === "response"
        ? "done"
        : "running";

  return (
    <EventCard
      title={normalized.title}
      kind={normalized.kind}
      tone={tone}
      time={displayTime(item.timestamp)}
      summary={normalized.summary}
      content={normalized.content}
      raw={payload}
      index={index}
      showRawDetails={showRawDetails}
    />
  );
}

// === 主组件 ===

export default function ChatPanel({
  conversations,
  expandDetails,
}: {
  conversations: ConversationView[];
  expandDetails: boolean;
}) {
  const streamRef = React.useRef<HTMLElement | null>(null);
  const timelineItems = React.useMemo<TimelineItem[]>(
    () => buildTraceTimelineItems(conversations),
    [conversations],
  );
  const scrollKey = React.useMemo(() => {
    const last = timelineItems[timelineItems.length - 1];
    if (!last) {
      return "empty";
    }
    if (last.kind === "aggregated_text") {
      return `${last.id}:${last.text.length}:${last.active}`;
    }
    if (last.kind === "aggregated_tool") {
      return `${last.id}:${last.inputText.length}:${last.resultText.length}`;
    }
    return `${last.id}:${timelineItems.length}`;
  }, [timelineItems]);

  React.useEffect(() => {
    const stream = streamRef.current;
    if (!stream) {
      return;
    }
    stream.scrollTo({
      top: stream.scrollHeight,
      behavior: "smooth",
    });
  }, [scrollKey]);

  return (
    <section
      ref={streamRef}
      className="chat-stream"
      data-expand-details={String(expandDetails)}
    >
      {timelineItems.length === 0 ? (
        <div className="chat-stream-blank" aria-hidden="true" />
      ) : (
        <>
          {timelineItems.map((item, index) => (
            <TimelineCard
              key={item.id}
              item={item}
              index={index}
              showRawDetails={expandDetails}
            />
          ))}
        </>
      )}
      <div className="event-stream-bottom-spacer" />
    </section>
  );
}
