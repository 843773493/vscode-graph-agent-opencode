import { formatDateTime } from "../utils/format";
import type { FrontendReceivedEvent } from "../types/frontend";

const DATA_IMAGE_PREFIX = "data:image/";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function redactLargeData(value: unknown): unknown {
  if (typeof value === "string") {
    if (value.startsWith(DATA_IMAGE_PREFIX)) {
      const commaIndex = value.indexOf(",");
      const header = commaIndex >= 0 ? value.slice(0, commaIndex) : DATA_IMAGE_PREFIX;
      const payloadLength = commaIndex >= 0 ? value.length - commaIndex - 1 : value.length;
      return `${header},<base64 ${payloadLength} chars redacted>`;
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map(redactLargeData);
  }
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, redactLargeData(item)]),
    );
  }
  return value;
}

function prettyJson(value: unknown): string {
  return JSON.stringify(redactLargeData(value), null, 2) ?? "";
}

function sourceLabel(source: FrontendReceivedEvent["source"]): string {
  if (source === "frontend") return "前端";
  if (source === "initial_load") return "初始加载";
  if (source === "pending_poll") return "运行轮询";
  if (source === "sse") return "SSE";
  if (source === "terminal_refresh") return "结束刷新";
  return source;
}

function eventPayload(item: FrontendReceivedEvent): Record<string, unknown> {
  if (item.kind === "frontend") {
    return item.payload ?? {};
  }
  return item.event.raw?.payload ?? item.event.payload ?? {};
}

function eventType(item: FrontendReceivedEvent): string {
  return item.kind === "frontend" ? item.type : item.event.type;
}

function eventTimestamp(item: FrontendReceivedEvent): string {
  return item.kind === "frontend" ? item.receivedAt : item.event.timestamp;
}

function eventSessionId(item: FrontendReceivedEvent): string {
  return item.sessionId;
}

type EventQueueDisplayItem =
  | { kind: "event"; item: FrontendReceivedEvent }
  | { kind: "text_delta_group"; items: FrontendReceivedEvent[] };

function textDeltaGroupKey(item: FrontendReceivedEvent): string {
  if (item.kind !== "trace" || item.event.type !== "text_delta") {
    return "";
  }

  const event = item.event;
  const payload = eventPayload(item);
  return [
    item.sessionId,
    item.source,
    event.job_id,
    event.step_id ?? "",
    event.agent_id ?? "",
    event.phase ?? "",
    payload.kind ?? "",
    payload.phase ?? "",
  ].join("|");
}

function buildDisplayItems(items: FrontendReceivedEvent[]): EventQueueDisplayItem[] {
  const result: EventQueueDisplayItem[] = [];
  let pendingTextDeltas: FrontendReceivedEvent[] = [];
  let pendingKey = "";

  const flushPending = () => {
    if (pendingTextDeltas.length === 0) {
      return;
    }
    if (pendingTextDeltas.length === 1) {
      result.push({ kind: "event", item: pendingTextDeltas[0] });
    } else {
      result.push({ kind: "text_delta_group", items: pendingTextDeltas });
    }
    pendingTextDeltas = [];
    pendingKey = "";
  };

  for (const item of items) {
    const key = textDeltaGroupKey(item);
    if (key) {
      if (pendingTextDeltas.length > 0 && pendingKey !== key) {
        flushPending();
      }
      pendingTextDeltas.push(item);
      pendingKey = key;
      continue;
    }

    flushPending();
    result.push({ kind: "event", item });
  }

  flushPending();
  return result;
}

function textDeltaText(item: FrontendReceivedEvent): string {
  const payload = eventPayload(item);
  const text = payload.text;
  return typeof text === "string" ? text : "";
}

function textDeltaKind(item: FrontendReceivedEvent): string {
  const payload = eventPayload(item);
  const kind = payload.kind ?? payload.phase;
  return typeof kind === "string" ? kind : "";
}

function attachmentNames(payload: Record<string, unknown>): string[] {
  const attachments = payload.attachments;
  if (!Array.isArray(attachments)) {
    return [];
  }
  return attachments.flatMap((attachment) => {
    if (!isRecord(attachment)) {
      return [];
    }
    const name = attachment.name ?? attachment.file_id;
    return typeof name === "string" && name ? [name] : [];
  });
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
  const jobId =
    event?.job_id && event.job_id !== "unknown_job" ? event.job_id : "";

  return (
    <article className={`event-queue-card event-type-${type}`}>
      <div className="event-queue-card-head">
        <div className="event-queue-title-row">
          <span className="event-queue-index">#{index + 1}</span>
          <span className="event-queue-type">
            {item.kind === "frontend" ? item.title : type}
          </span>
          <span className="event-queue-source">{sourceLabel(item.source)}</span>
        </div>
        <div className="event-queue-time">
          {eventTime ? <span>事件 {eventTime}</span> : null}
          {receivedTime ? <span>收到 {receivedTime}</span> : null}
        </div>
      </div>

      <div className="event-queue-meta">
        <span>session: {eventSessionId(item)}</span>
        {jobId ? <span>job: {jobId}</span> : null}
        {event?.agent_id ? <span>agent: {event.agent_id}</span> : null}
        {event?.phase ? <span>phase: {event.phase}</span> : null}
        {event?.status ? <span>status: {event.status}</span> : null}
        {item.kind === "frontend" && item.detail ? (
          <span>{item.detail}</span>
        ) : null}
      </div>

      {type === "message_created" ? (
        <MessageCreatedSummary payload={payload} />
      ) : null}

      <details className="event-queue-detail" open={index >= 0 && index < 3}>
        <summary>payload</summary>
        <pre>{prettyJson(payload)}</pre>
      </details>

      <details className="event-queue-detail">
        <summary>完整事件</summary>
        <pre>{prettyJson(item.kind === "trace" ? item.event : item)}</pre>
      </details>
    </article>
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
  const jobId =
    event?.job_id && event.job_id !== "unknown_job" ? event.job_id : "";
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
    <article className="event-queue-card event-type-text_delta event-queue-group-card">
      <div className="event-queue-card-head">
        <div className="event-queue-title-row">
          <span className="event-queue-index">#{index + 1}</span>
          <span className="event-queue-type">text_delta × {items.length}</span>
          <span className="event-queue-source">{sourceLabel(first.source)}</span>
        </div>
        <div className="event-queue-time">
          {eventTime ? <span>首条 {eventTime}</span> : null}
          {receivedTime ? <span>末条收到 {receivedTime}</span> : null}
        </div>
      </div>

      <div className="event-queue-meta">
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

      <details className="event-queue-detail">
        <summary>合并文本</summary>
        <pre>{prettyJson(payloadPreview)}</pre>
      </details>

      <details className="event-queue-detail">
        <summary>原始 text_delta 列表</summary>
        <pre>
          {prettyJson(
            items.map((item) =>
              item.kind === "trace" ? item.event : item,
            ),
          )}
        </pre>
      </details>
    </article>
  );
}

export default function EventQueuePanel({
  items,
  limit,
  sessionId,
}: {
  items: FrontendReceivedEvent[];
  limit: number;
  sessionId: string;
}) {
  const visibleItems = items.filter((item) => item.sessionId === sessionId);
  const displayItems = buildDisplayItems(visibleItems);

  return (
    <section className="event-queue-panel">
      <div className="event-queue-header">
        <div className="event-queue-title">事件视图</div>
        <div className="event-queue-header-meta">
          <span>{displayItems.length} events</span>
          <span>原始 {visibleItems.length}</span>
          <span>上限 {limit}</span>
          <span>{sessionId}</span>
        </div>
      </div>

      {visibleItems.length > 0 ? (
        <div className="event-queue-list">
          {displayItems.map((item, index) =>
            item.kind === "text_delta_group" ? (
              <TextDeltaGroupCard
                key={`text-delta-group-${item.items[0]?.id ?? index}`}
                items={item.items}
                index={index}
              />
            ) : (
              <EventQueueCard key={item.item.id} item={item.item} index={index} />
            ),
          )}
        </div>
      ) : (
        <div className="empty-state">当前会话还没有前端收到的事件</div>
      )}
    </section>
  );
}
