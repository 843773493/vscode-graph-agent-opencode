import React from "react";
import { formatDateTime } from "../utils/format";
import { prettyJson } from "../utils/jsonDisplay";
import type { FrontendReceivedEvent } from "../types/frontend";
import {
  attachmentNames,
  buildDisplayItems,
  buildKeyTraceSummary,
  eventPayload,
  eventSessionId,
  eventTimestamp,
  eventType,
  orderedTraceItems,
  textDeltaKind,
  textDeltaText,
  toolEventSummary,
} from "../state/eventQueueDisplay";
import { useEventQueuePagination } from "./eventQueue/useEventQueuePagination";

export const INITIAL_VISIBLE_EVENT_COUNT = 30;

function sourceLabel(source: FrontendReceivedEvent["source"]): string {
  if (source === "frontend") return "页面操作";
  if (source === "initial_load") return "历史加载";
  if (source === "recovery") return "连接恢复";
  if (source === "sse") return "实时事件";
  return source;
}

function MessageCreatedSummary({
  payload,
}: {
  payload: Record<string, unknown>;
}) {
  const content = typeof payload.content === "string" ? payload.content : "";
  const names = attachmentNames(payload);
  if (!content && names.length === 0) {
    return null;
  }
  return (
    <div className="event-queue-input-summary">
      {content ? <div className="event-queue-input-text">{content}</div> : null}
      {names.length > 0 ? (
        <div className="event-queue-attachments">
          {names.map((name) => (
            <span key={name}>附件: {name}</span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function EventQueueCard({ item, index }: { item: FrontendReceivedEvent; index: number }) {
  const payload = eventPayload(item);
  const type = eventType(item);
  const eventTime = formatDateTime(eventTimestamp(item));
  const receivedTime = formatDateTime(item.receivedAt);
  const event = item.kind === "trace" ? item.event : null;
  const jobId = event?.job_id ?? "";
  const toolSummary = toolEventSummary(type, payload);
  const compactContent = item.kind === "frontend"
    ? item.detail || item.title
    : item.event.content || "";

  return (
    <details className={`panel-card event-queue-card event-type-${type}`}>
      <summary className="event-queue-card-summary">
        <div className="panel-title-row">
          <span className="panel-index">#{index + 1}</span>
          <span className="panel-type">
            {item.kind === "frontend" ? item.title : type}
          </span>
          <span className="panel-pill event-queue-source">{sourceLabel(item.source)}</span>
        </div>
        {compactContent ? (
          <span className="event-queue-compact-content">{compactContent}</span>
        ) : null}
        <div className="panel-time">
          {eventTime ? <span>事件 {eventTime}</span> : null}
          {receivedTime ? <span>收到 {receivedTime}</span> : null}
        </div>
      </summary>

      <div className="event-queue-card-expanded">
        <div className="panel-meta">
          <span>session: {eventSessionId(item)}</span>
          {jobId ? <span>job: {jobId}</span> : null}
          {event?.agent_id ? <span>agent: {event.agent_id}</span> : null}
          {event?.phase ? <span>phase: {event.phase}</span> : null}
          {event?.status ? <span>status: {event.status}</span> : null}
          {toolSummary ? <span>tool: {toolSummary.displayToolName}</span> : null}
          {toolSummary?.skillNames.length ? (
            <span>skill: {toolSummary.skillNames.join(", ")}</span>
          ) : null}
          {item.kind === "frontend" && item.detail ? (
            <span>{item.detail}</span>
          ) : null}
        </div>

        {toolSummary ? (
          <div className="event-queue-tool-summary">
            <span>{type === "tool_call_start" ? "输入" : "结果"}</span>
            <code>{toolSummary.summary}</code>
          </div>
        ) : null}

        {type === "message_created" ? (
          <MessageCreatedSummary payload={payload} />
        ) : null}

        <details className="panel-detail">
          <summary>payload</summary>
          <pre>{prettyJson(payload)}</pre>
        </details>

        <details className="panel-detail">
          <summary>完整事件</summary>
          <pre>{prettyJson(item.kind === "trace" ? item.event : item)}</pre>
        </details>
      </div>
    </details>
  );
}

function TextDeltaGroupCard({
  items,
  index,
}: {
  items: FrontendReceivedEvent[];
  index: number;
}) {
  const first = items[0];
  const last = items[items.length - 1];
  const event = first?.kind === "trace" ? first.event : null;
  const jobId = event?.job_id ?? "";
  const eventTime = first ? formatDateTime(eventTimestamp(first)) : "";
  const receivedTime = last ? formatDateTime(last.receivedAt) : "";
  const mergedText = items.map(textDeltaText).join("");
  const deltaKinds = Array.from(
    new Set(items.map(textDeltaKind).filter((kind) => kind.length > 0)),
  );
  const stats = {
    delta_count: items.length,
    merged_text_length: mergedText.length,
    source: first.source,
    first_event_at: eventTimestamp(first),
    last_event_at: last ? eventTimestamp(last) : "",
    first_received_at: first.receivedAt,
    last_received_at: last?.receivedAt ?? "",
    delta_kinds: deltaKinds,
  };
  const payloadPreview =
    mergedText.length > 0
      ? { text: mergedText, stats }
      : { stats };

  return (
    <details className="panel-card event-queue-card event-type-text_delta event-queue-group-card">
      <summary className="event-queue-card-summary">
        <div className="panel-title-row">
          <span className="panel-index">#{index + 1}</span>
          <span className="panel-type">text_delta × {items.length}</span>
          <span className="panel-pill event-queue-source">{sourceLabel(first.source)}</span>
        </div>
        {mergedText ? (
          <span className="event-queue-compact-content">{mergedText}</span>
        ) : null}
        <div className="panel-time">
          {eventTime ? <span>首条 {eventTime}</span> : null}
          {receivedTime ? <span>末条收到 {receivedTime}</span> : null}
        </div>
      </summary>

      <div className="event-queue-card-expanded">
        <div className="panel-meta">
          <span>session: {eventSessionId(first)}</span>
          {jobId ? <span>job: {jobId}</span> : null}
          {event?.agent_id ? <span>agent: {event.agent_id}</span> : null}
          {event?.phase ? <span>phase: {event.phase}</span> : null}
        </div>

        <div className="event-queue-stats">
          <span>delta 数: {stats.delta_count}</span>
          <span>合并字符: {stats.merged_text_length}</span>
          {deltaKinds.length > 0 ? <span>kind: {deltaKinds.join(", ")}</span> : null}
        </div>

        <details className="panel-detail">
          <summary>合并文本</summary>
          <pre>{prettyJson(payloadPreview)}</pre>
        </details>

        <details className="panel-detail">
          <summary>原始 text_delta 列表</summary>
          <pre>
            {prettyJson(
              items.map((item) =>
                item.kind === "trace" ? item.event : item,
              ),
            )}
          </pre>
        </details>
      </div>
    </details>
  );
}

function KeyTraceSummary({
  readSkills,
  keyFlowToolCalls,
  keyFlowToolResults,
  finalText,
}: ReturnType<typeof buildKeyTraceSummary>) {
  if (
    readSkills.length === 0 &&
    keyFlowToolCalls.length === 0 &&
    keyFlowToolResults.length === 0 &&
    !finalText
  ) {
    return null;
  }

  return (
    <div className="event-queue-key-trace">
      <div className="event-queue-key-trace-title">关键链路</div>
      <div className="event-queue-key-trace-items">
        {readSkills.length > 0 ? (
          <span>已读取 skill：{readSkills.join("、")}</span>
        ) : null}
        {keyFlowToolCalls.map((call) => (
          <span key={`${call.invocationToolName ?? ""}:${call.toolName}`}>
            调用扩展工具：{call.invocationToolName ? `${call.invocationToolName} -> ` : ""}
            {call.toolName}
            {call.skillNames.length > 0
              ? `（${call.skillNames.join("、")} 记录）`
              : ""}
          </span>
        ))}
        {keyFlowToolResults.map((result) => (
          <span key={`${result.invocationToolName ?? ""}:${result.toolName}:result`}>
            扩展工具结果：{result.invocationToolName ? `${result.invocationToolName} -> ` : ""}
            {result.toolName} -&gt; {result.resultText}
          </span>
        ))}
        {finalText ? <span>最终文本：{finalText}</span> : null}
      </div>
    </div>
  );
}

export default function EventQueuePanel({
  items,
  limit,
  sessionId,
  active,
  historyLoading,
  historyLoadingOlder,
  historyHasMore,
  historyError,
  onLoadOlderHistory,
  onRetryHistory,
}: {
  items: FrontendReceivedEvent[];
  limit: number;
  sessionId: string;
  active: boolean;
  historyLoading: boolean;
  historyLoadingOlder: boolean;
  historyHasMore: boolean;
  historyError: string | null;
  onLoadOlderHistory: () => Promise<number>;
  onRetryHistory: () => void;
}) {
  const visibleItems = items.filter((item) => item.sessionId === sessionId);
  const traceItems = orderedTraceItems(visibleItems);
  const hiddenEventCount = Math.max(visibleItems.length - traceItems.length, 0);
  const displayItems = buildDisplayItems(traceItems);
  const {
    firstVisibleIndex,
    handleListScroll,
    listRef,
    revealOlderEvents,
    visibleCount,
  } = useEventQueuePagination({
    active,
    sessionId,
    displayItemCount: displayItems.length,
    historyLoading,
    historyLoadingOlder,
    historyHasMore,
    onLoadOlderHistory,
  });
  const renderedItems = displayItems.slice(firstVisibleIndex);
  const keyTraceSummary = buildKeyTraceSummary(traceItems);
  const hasOlderEvents = firstVisibleIndex > 0 || historyHasMore;

  return (
    <section className="panel-view event-queue-panel">
      <div className="panel-header">
        <div className="panel-title">事件视图</div>
        <div className="panel-header-meta">
          <span>{displayItems.length} events</span>
          <span>已显示 {renderedItems.length}</span>
          <span>权威 trace</span>
          {hiddenEventCount > 0 ? <span>已隐藏 {hiddenEventCount} 条传输/轮询重复事件</span> : null}
          <span>流程顺序</span>
          <span>上限 {limit}</span>
          <span>{sessionId}</span>
        </div>
      </div>

      {historyError ? (
        <div className="event-queue-history-error" role="alert">
          <span>{historyError}</span>
          <button type="button" onClick={onRetryHistory}>重新加载事件历史</button>
        </div>
      ) : null}

      {traceItems.length > 0 ? (
        <>
          <KeyTraceSummary {...keyTraceSummary} />
          <div
            className="panel-list"
            ref={listRef}
            onScroll={handleListScroll}
          >
            {hasOlderEvents ? (
              <button
                type="button"
                className="event-queue-load-older"
                disabled={historyLoading || historyLoadingOlder}
                onClick={revealOlderEvents}
              >
                {firstVisibleIndex > 0
                  ? "向上滚动加载更早事件"
                  : historyLoadingOlder
                    ? "正在读取更早事件…"
                    : "加载更早事件"}
              </button>
            ) : null}
            {renderedItems.map((item, index) => {
              const absoluteIndex = firstVisibleIndex + index;
              return (
                item.kind === "text_delta_group" ? (
                  <TextDeltaGroupCard
                    key={`text-delta-group-${item.items[0]?.id ?? index}`}
                    items={item.items}
                    index={absoluteIndex}
                  />
                ) : (
                  <EventQueueCard
                    key={item.item.id}
                    item={item.item}
                    index={absoluteIndex}
                  />
                )
              );
            })}
          </div>
        </>
      ) : (
        <div className="empty-state">
          {historyLoading ? "正在读取事件历史…" : "当前会话还没有事件"}
        </div>
      )}
    </section>
  );
}
