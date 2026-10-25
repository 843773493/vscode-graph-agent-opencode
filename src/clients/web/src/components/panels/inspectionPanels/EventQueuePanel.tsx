import React from "react";
import { formatDateTime } from "../../../utils/format";
import {
  boundedDisplayString,
  LARGE_STRING_DISPLAY_LIMIT,
  prettyJson,
} from "../../../utils/jsonDisplay";
import type { FrontendReceivedEvent } from "../../../types/frontend";
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
} from "../../../state/display/eventQueueDisplay";
import { useEventQueuePagination } from "../../eventQueue/useEventQueuePagination";

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
  const content = boundedDisplayString(
    typeof payload.content === "string" ? payload.content : "",
  );
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

function useLazyExpandedContent() {
  const [hasOpened, setHasOpened] = React.useState(false);
  const handleToggle = React.useCallback((event: React.SyntheticEvent<HTMLDetailsElement>) => {
    if (event.currentTarget.open) setHasOpened(true);
  }, []);
  return { hasOpened, handleToggle };
}

function LazyPrettyJsonDetails({
  title,
  getValue,
}: {
  title: string;
  getValue: () => unknown;
}) {
  const { hasOpened, handleToggle } = useLazyExpandedContent();
  const json = React.useMemo(
    () => hasOpened ? prettyJson(getValue()) : "",
    [getValue, hasOpened],
  );

  return (
    <details className="panel-detail" onToggle={handleToggle}>
      <summary>{title}</summary>
      {hasOpened ? <pre>{json}</pre> : null}
    </details>
  );
}

const EventQueueCard = React.memo(function EventQueueCard({
  item,
  index,
}: {
  item: FrontendReceivedEvent;
  index: number;
}) {
  const payload = eventPayload(item);
  const type = eventType(item);
  const eventTime = formatDateTime(eventTimestamp(item));
  const receivedTime = formatDateTime(item.receivedAt);
  const event = item.kind === "trace" ? item.event : null;
  const jobId = event?.job_id ?? "";
  const { hasOpened, handleToggle } = useLazyExpandedContent();
  const toolSummary = React.useMemo(
    () => hasOpened ? toolEventSummary(type, payload) : null,
    [hasOpened, payload, type],
  );
  const compactContent = boundedDisplayString(
    item.kind === "frontend"
      ? item.detail || item.title
      : item.event.content || "",
  );
  const getPayload = React.useCallback(() => payload, [payload]);
  const getEvent = React.useCallback(
    () => item.kind === "trace" ? item.event : item,
    [item],
  );

  return (
    <details
      className={`panel-card event-queue-card event-type-${type}`}
      onToggle={handleToggle}
    >
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

      {hasOpened ? (
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
              <span>{boundedDisplayString(item.detail)}</span>
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

          <LazyPrettyJsonDetails title="payload" getValue={getPayload} />
          <LazyPrettyJsonDetails title="完整事件" getValue={getEvent} />
        </div>
      ) : null}
    </details>
  );
});

function summarizeTextDeltaGroup(items: FrontendReceivedEvent[]) {
  const first = items[0];
  const last = items[items.length - 1];
  const previewParts: string[] = [];
  const deltaKinds: string[] = [];
  const seenDeltaKinds = new Set<string>();
  let previewLength = 0;
  let mergedTextLength = 0;

  for (const item of items) {
    const text = textDeltaText(item);
    mergedTextLength += text.length;
    const remainingPreviewLength = LARGE_STRING_DISPLAY_LIMIT - previewLength;
    if (remainingPreviewLength > 0) {
      const previewPart = text.slice(0, remainingPreviewLength);
      previewParts.push(previewPart);
      previewLength += previewPart.length;
    }

    const kind = textDeltaKind(item);
    if (kind && !seenDeltaKinds.has(kind)) {
      seenDeltaKinds.add(kind);
      deltaKinds.push(kind);
    }
  }

  const preview = previewParts.join("");
  return {
    previewText: mergedTextLength > LARGE_STRING_DISPLAY_LIMIT
      ? `${preview}…（已截断展示，原文 ${mergedTextLength} 字符）`
      : preview,
    stats: {
      delta_count: items.length,
      merged_text_length: mergedTextLength,
      source: first.source,
      first_event_at: eventTimestamp(first),
      last_event_at: eventTimestamp(last),
      first_received_at: first.receivedAt,
      last_received_at: last.receivedAt,
      delta_kinds: deltaKinds,
    },
  };
}

const TextDeltaGroupCard = React.memo(function TextDeltaGroupCard({
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
  const { previewText, stats } = React.useMemo(
    () => summarizeTextDeltaGroup(items),
    [items],
  );
  const getMergedPayload = React.useCallback(() => {
    const mergedText = items.map(textDeltaText).join("");
    return mergedText.length > 0 ? { text: mergedText, stats } : { stats };
  }, [items, stats]);
  const getOriginalEvents = React.useCallback(
    () => items.map((item) => item.kind === "trace" ? item.event : item),
    [items],
  );
  const { hasOpened, handleToggle } = useLazyExpandedContent();

  return (
    <details
      className="panel-card event-queue-card event-type-text_delta event-queue-group-card"
      onToggle={handleToggle}
    >
      <summary className="event-queue-card-summary">
        <div className="panel-title-row">
          <span className="panel-index">#{index + 1}</span>
          <span className="panel-type">text_delta × {items.length}</span>
          <span className="panel-pill event-queue-source">{sourceLabel(first.source)}</span>
        </div>
        {previewText ? (
          <span className="event-queue-compact-content">
            {previewText}
          </span>
        ) : null}
        <div className="panel-time">
          {eventTime ? <span>首条 {eventTime}</span> : null}
          {receivedTime ? <span>末条收到 {receivedTime}</span> : null}
        </div>
      </summary>

      {hasOpened ? (
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
            {stats.delta_kinds.length > 0 ? (
              <span>kind: {stats.delta_kinds.join(", ")}</span>
            ) : null}
          </div>

          <LazyPrettyJsonDetails title="合并文本" getValue={getMergedPayload} />
          <LazyPrettyJsonDetails title="原始 text_delta 列表" getValue={getOriginalEvents} />
        </div>
      ) : null}
    </details>
  );
});

const KeyTraceSummary = React.memo(function KeyTraceSummary({
  acceptedSkillLoads,
  keyFlowToolCalls,
  keyFlowToolResults,
  finalText,
}: ReturnType<typeof buildKeyTraceSummary>) {
  if (
    acceptedSkillLoads.length === 0 &&
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
        {acceptedSkillLoads.length > 0 ? (
          <span>已接受的 Skill 加载：{acceptedSkillLoads.join("、")}</span>
        ) : null}
        {keyFlowToolCalls.map((call) => (
          <span key={`${call.invocationToolName ?? ""}:${call.toolName}:${call.skillNames.join(":")}`}>
            调用扩展工具：{call.invocationToolName ? `${call.invocationToolName} -> ` : ""}
            {call.toolName}
            {call.skillNames.length > 0
              ? `（${call.skillNames.join("、")} 记录）`
              : ""}
          </span>
        ))}
        {keyFlowToolResults.map((result) => (
          <span key={`${result.invocationToolName ?? ""}:${result.toolName}:${result.skillNames.join(":")}:${result.resultText}`}>
            扩展工具结果：{result.invocationToolName ? `${result.invocationToolName} -> ` : ""}
            {result.toolName} -&gt; {result.resultText}
          </span>
        ))}
        {finalText ? <span>最终文本：{finalText}</span> : null}
      </div>
    </div>
  );
});

export default function EventQueuePanel({
  items,
  limit,
  sessionId,
  scopeKey,
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
  scopeKey: string;
  active: boolean;
  historyLoading: boolean;
  historyLoadingOlder: boolean;
  historyHasMore: boolean;
  historyError: string | null;
  onLoadOlderHistory: () => Promise<number>;
  onRetryHistory: () => void;
}) {
  const eventView = React.useMemo(() => {
    const sessionItems = items.filter((item) => item.sessionId === sessionId);
    const traceItems = orderedTraceItems(sessionItems);
    return {
      traceItems,
      hiddenEventCount: Math.max(sessionItems.length - traceItems.length, 0),
      displayItems: buildDisplayItems(traceItems),
      keyTraceSummary: buildKeyTraceSummary(traceItems),
    };
  }, [items, sessionId]);
  const { traceItems, hiddenEventCount, displayItems, keyTraceSummary } = eventView;
  const {
    firstVisibleIndex,
    handleListScroll,
    listRef,
    revealOlderEvents,
  } = useEventQueuePagination({
    active,
    scopeKey,
    displayItemCount: displayItems.length,
    historyLoading,
    historyLoadingOlder,
    historyHasMore,
    onLoadOlderHistory,
  });
  const renderedItems = displayItems.slice(firstVisibleIndex);
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
        historyError ? null : (
          <div className="empty-state">
            {historyLoading ? "正在读取事件历史…" : "当前会话还没有事件"}
          </div>
        )
      )}
    </section>
  );
}
