import React from "react";
import type { AttachmentRef } from "../types/backend";
import {
  buildTraceTimelineItems,
  normalizeTraceData,
  type TimelineItem,
} from "../state/chatTimeline";
import type { ConversationView } from "../types/frontend";
import { escapeHtml, formatDateTime } from "../utils/format";
import { renderMarkdown } from "../utils/markdown";
import AttachmentList from "./AttachmentList";

function displayTime(value: unknown): string {
  return formatDateTime(value) || "now";
}

// === 事件卡片组件 ===

function EventCard({
  title,
  kind,
  tone,
  time,
  summary,
  content,
  collapsedContent,
  raw,
  index,
  attachments = [],
}: {
  title: string;
  kind:
    | "message"
    | "trace"
    | "system"
    | "response"
    | "thought"
    | "tool_call"
    | "tool_result"
    | "error";
  tone: "running" | "done" | "danger";
  time: string;
  summary: string;
  content: string;
  collapsedContent?: string;
  raw: Record<string, unknown>;
  index: number;
  attachments?: AttachmentRef[];
}): React.ReactNode {
  const [open, setOpen] = React.useState(false);
  const collapsedText =
    collapsedContent ?? (content || summary || "（无可读内容）");

  return (
    <article
      className={`event-card event-card-${kind} tone-${tone} ${open ? "is-open" : "is-collapsed"}`}
    >
      <div className="event-card-head">
        <div className="event-card-title-row">
          <span
            className={`event-card-indicator event-card-indicator-${tone}`}
          />
          <span className="event-card-title">{escapeHtml(title)}</span>
        </div>
        <div className="event-card-head-right">
          <span className="badge neutral event-card-time">
            {escapeHtml(time || `#${index + 1}`)}
          </span>
          <button
            type="button"
            className="event-card-toggle"
            aria-expanded={open}
            aria-label={open ? "折叠" : "展开"}
            onClick={() => setOpen((prev) => !prev)}
          >
            {open ? "−" : "+"}
          </button>
        </div>
      </div>
      {!open ? (
        <>
          <div className="event-card-summary event-card-summary-collapsed">
            {escapeHtml(collapsedText)}
          </div>
          {attachments.length > 0 && (
            <AttachmentList attachments={attachments} />
          )}
        </>
      ) : (
        <div className="event-card-body">
          {summary && (
            <div className="event-card-summary">{escapeHtml(summary)}</div>
          )}
          {content ? (
            <div
              className="event-card-content"
              dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }}
            />
          ) : attachments.length === 0 ? (
            <div className="event-card-empty">（无可读内容）</div>
          ) : null}
          {attachments.length > 0 && (
            <AttachmentList attachments={attachments} />
          )}
          <details className="event-card-details">
            <summary>原始数据</summary>
            <pre>{escapeHtml(JSON.stringify(raw, null, 2))}</pre>
          </details>
        </div>
      )}
    </article>
  );
}

// === 子卡片组件 ===

function StatusCard({
  item,
  index,
}: {
  item: Extract<TimelineItem, { kind: "status" }>;
  index: number;
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
    />
  );
}

function AggregatedTextCard({
  item,
  index,
}: {
  item: Extract<TimelineItem, { kind: "aggregated_text" }>;
  index: number;
}): React.ReactNode {
  const text = item.text.trim();
  const isReasoning = item.phase === "reasoning";
  const title = isReasoning ? "推理过程" : item.active ? "回复生成中" : "最终回复";
  const kind = isReasoning ? "thought" : "response";
  const tone = item.active || isReasoning ? "running" : "done";
  const phaseLabel = isReasoning ? "推理" : "回复";
  return (
    <EventCard
      title={title}
      kind={kind}
      tone={tone}
      time={displayTime(item.timestamp)}
      summary={`${item.eventCount} 个${phaseLabel}事件已合并`}
      content={text || "（空）"}
      raw={{
        aggregated: true,
        eventCount: item.eventCount,
        phase: item.phase,
        active: item.active,
        text,
        rawEvents: item.rawEvents,
      }}
      index={index}
    />
  );
}

function AggregatedToolCard({
  item,
  index,
}: {
  item: Extract<TimelineItem, { kind: "aggregated_tool" }>;
  index: number;
}): React.ReactNode {
  const { toolName, inputText, resultText, timestamp } = item;
  const content =
    [
      inputText ? `**输入参数**\n\`\`\`\n${inputText}\n\`\`\`` : "",
      resultText ? `**执行结果**\n\`\`\`\n${resultText}\n\`\`\`` : "",
    ]
      .filter(Boolean)
      .join("\n\n") || "（无详情）";

  return (
    <EventCard
      title={`🔧 ${toolName}`}
      kind="tool_call"
      tone="done"
      time={displayTime(timestamp)}
      summary={toolName}
      content={content}
      collapsedContent={`${toolName} 执行完成`}
      raw={{
        toolName,
        inputText,
        resultText,
        rawStart: item.rawStart,
        rawEnd: item.rawEnd,
      }}
      index={index}
    />
  );
}

function ConversationMarker({
  item,
}: {
  item: Extract<TimelineItem, { kind: "conversation_marker" }>;
}): React.ReactNode {
  return (
    <div className="conversation-marker">
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
}: {
  item: TimelineItem;
  index: number;
}): React.ReactNode {
  if (item.kind === "status") {
    return <StatusCard item={item} index={index} />;
  }

  if (item.kind === "conversation_marker") {
    return <ConversationMarker item={item} />;
  }

  if (item.kind === "aggregated_text") {
    return <AggregatedTextCard item={item} index={index} />;
  }

  if (item.kind === "aggregated_tool") {
    return <AggregatedToolCard item={item} index={index} />;
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
        content={isUser ? item.content : trimmed}
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
  const timelineItems = React.useMemo<TimelineItem[]>(
    () => buildTraceTimelineItems(conversations),
    [conversations],
  );

  return (
    <section
      className="chat-stream"
      data-expand-details={String(expandDetails)}
    >
      {timelineItems.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state-title">对话区</div>
          <div>输入消息后，这里会显示完整的会话卡片、回复和 trace 细节。</div>
        </div>
      ) : (
        timelineItems.map((item, index) => (
          <TimelineCard key={item.id} item={item} index={index} />
        ))
      )}
      <div className="event-stream-bottom-spacer" />
    </section>
  );
}
