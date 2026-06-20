import React from 'react';
import type { Message, TraceEvent } from '../types/backend';
import type { ConversationView } from '../types/frontend';
import { escapeHtml, formatTime } from '../utils/format';
import { renderMarkdown } from '../utils/markdown';

function displayTime(value: unknown): string {
  return formatTime(value) || 'now';
}

function getRequiredString(payload: Record<string, unknown>, key: string, eventType: string): string {
  const value = payload[key];
  if (typeof value !== 'string' || value.trim() === '') {
    const detail = JSON.stringify(payload, null, 2);
    console.error(`事件结构异常 ${eventType}`, detail);
    throw new Error(`事件 ${eventType} 缺少必需字段 ${key}\n完整结构:\n${detail}`);
  }
  return value;
}

function getOptionalString(payload: Record<string, unknown>, key: string): string {
  const value = payload[key];
  return typeof value === 'string' ? value : '';
}

type TimelineItem =
  | { kind: 'message'; id: string; role: Message['role']; content: string; createdAt: string | null; metadata: Record<string, unknown> }
  | { kind: 'trace'; id: string; eventType: string; payload: Record<string, unknown>; timestamp: string | null }
  | { kind: 'streaming'; id: string; content: string }
  | { kind: 'aggregated_text'; id: string; text: string; phase: string; timestamp: string | null; eventCount: number; rawEvents: Array<{ type: string; payload: Record<string, unknown> }> }
  | { kind: 'aggregated_tool'; id: string; toolName: string; inputText: string; resultText: string; timestamp: string | null; rawStart: Record<string, unknown>; rawEnd: Record<string, unknown> }
  | { kind: 'conversation_marker'; id: string; label: string };

function normalizeTraceData(
  eventType: string,
  payload: Record<string, unknown>,
): {
  kind: 'thought' | 'tool_call' | 'tool_result' | 'response' | 'system' | 'error';
  title: string;
  summary: string;
  content: string;
} {
  const type = String(eventType ?? '').toLowerCase();
  const message = getOptionalString(payload, 'message');
  const resultText = getOptionalString(payload, 'result');
  const toolName = getOptionalString(payload, 'tool_name');
  const modelName = getOptionalString(payload, 'model');
  const errorText = String(payload.error ?? payload.message ?? payload.detail ?? '').trim();

  if (type === 'tool_call_start') {
    const requiredToolName = getRequiredString(payload, 'tool_name', type);
    return {
      kind: 'tool_call',
      title: `调用工具 ${requiredToolName}`,
      summary: String(payload.phase ?? ''),
      content: message || resultText || `正在调用 ${requiredToolName}`,
    };
  }

  if (type === 'tool_call_end' || type === 'file_write') {
    const requiredToolName = type === 'tool_call_end' ? getRequiredString(payload, 'tool_name', type) : '';
    const requiredPath = type === 'file_write' ? getRequiredString(payload, 'path', type) : '';
    return {
      kind: 'tool_result',
      title: type === 'tool_call_end' ? `工具结果 ${requiredToolName}` : `文件写入 ${requiredPath}`,
      summary: type === 'tool_call_end' ? '工具执行完成' : '文件已写入',
      content: resultText || message || requiredPath,
    };
  }

  if (type === 'error' || type === 'job_failed' || type === 'job_cancelled') {
    return {
      kind: 'error',
      title: '执行异常',
      summary: errorText,
      content: String(payload.stack ?? payload.detail ?? payload.message ?? payload.error ?? errorText ?? ''),
    };
  }

  if (type === 'llm_empty_response') {
    const modelName = getOptionalString(payload, 'model');
    const msg = getOptionalString(payload, 'message');
    return {
      kind: 'error',
      title: 'LLM 空响应',
      summary: modelName ? `模型 ${modelName} 未返回内容` : '模型未返回任何内容',
      content: msg || 'Agent 已执行但 LLM 返回空响应。请检查模型配置或 API 连接。',
    };
  }

  if (type === 'system_reminder_injected') {
    const content = getOptionalString(payload, 'content');
    const position = getOptionalString(payload, 'position');
    return {
      kind: 'system',
      title: '系统提示',
      summary: position ? `注入位置: ${position}` : '',
      content: content || message || resultText,
    };
  }

  // 防御：model_call 是已废弃的事件类型，不应再出现。fallthrough 到通用 system 卡片。
  if (type === 'model_call') {
    console.warn('normalizeTraceData: 已废弃的事件类型 model_call 已收到, payload=', payload);
  }

  return {
    kind: 'system',
    title: `事件 ${eventType}`,
    summary: [toolName ? `工具: ${toolName}` : '', modelName ? `模型: ${modelName}` : '']
      .filter(Boolean)
      .join(' · '),
    content: message || resultText || errorText,
  };
}

// === 事件卡片组件 ===

function EventCard({
  title,
  kind,
  tone,
  time,
  summary,
  content,
  raw,
  index,
}: {
  title: string;
  kind: 'message' | 'trace' | 'system' | 'response' | 'thought' | 'tool_call' | 'tool_result' | 'error';
  tone: 'running' | 'done' | 'danger';
  time: string;
  summary: string;
  content: string;
  raw: Record<string, unknown>;
  index: number;
}): React.ReactNode {
  const [open, setOpen] = React.useState(false);

  return (
    <article className={`event-card event-card-${kind} tone-${tone} ${open ? 'is-open' : 'is-collapsed'}`}>
      <div className="event-card-head">
        <div className="event-card-title-row">
          <span className={`event-card-indicator event-card-indicator-${tone}`} />
          <span className="event-card-title">{escapeHtml(title)}</span>
        </div>
        <div className="event-card-head-right">
          <span className="badge neutral event-card-time">{escapeHtml(time || `#${index + 1}`)}</span>
          <button
            type="button"
            className="event-card-toggle"
            aria-expanded={open}
            aria-label={open ? '折叠' : '展开'}
            onClick={() => setOpen(prev => !prev)}
          >
            {open ? '−' : '+'}
          </button>
        </div>
      </div>
      {!open ? (
        <div className="event-card-summary event-card-summary-collapsed">
          {escapeHtml(content || summary || '（无可读内容）')}
        </div>
      ) : (
        <div className="event-card-body">
          {summary && <div className="event-card-summary">{escapeHtml(summary)}</div>}
          {content ? (
            <div className="event-card-content" dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }} />
          ) : (
            <div className="event-card-empty">（无可读内容）</div>
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

function StreamingCard({ content, index }: { content: string; index: number }): React.ReactNode {
  return (
    <EventCard
      title="Assistant 生成中"
      kind="response"
      tone="running"
      time={displayTime(new Date().toISOString())}
      summary=""
      content={content}
      raw={{ streaming: true, content }}
      index={index}
    />
  );
}

function AggregatedTextCard({
  item,
  index,
}: {
  item: Extract<TimelineItem, { kind: 'aggregated_text' }>;
  index: number;
}): React.ReactNode {
  const text = item.text.trim();
  const isReasoning = item.phase === 'reasoning';
  const title = isReasoning ? '🧠 推理过程' : '💬 最终回复';
  const kind = isReasoning ? 'thought' : 'response';
  const tone = isReasoning ? 'running' : 'done';
  const phaseLabel = isReasoning ? '推理' : '回复';
  return (
    <EventCard
      title={title}
      kind={kind}
      tone={tone}
      time={displayTime(item.timestamp)}
      summary={`${item.eventCount} 个${phaseLabel}事件已合并`}
      content={text || '（空）'}
      raw={{ aggregated: true, eventCount: item.eventCount, phase: item.phase, text, rawEvents: item.rawEvents }}
      index={index}
    />
  );
}

function AggregatedToolCard({
  item,
  index,
}: {
  item: Extract<TimelineItem, { kind: 'aggregated_tool' }>;
  index: number;
}): React.ReactNode {
  const { toolName, inputText, resultText, timestamp } = item;
  const content = [
    inputText ? `**输入参数**\n\`\`\`\n${inputText}\n\`\`\`` : '',
    resultText ? `**执行结果**\n\`\`\`\n${resultText}\n\`\`\`` : '',
  ]
    .filter(Boolean)
    .join('\n\n') || '（无详情）';

  return (
    <EventCard
      title={`🔧 ${toolName}`}
      kind="tool_call"
      tone="done"
      time={displayTime(timestamp)}
      summary={toolName}
      content={content}
      raw={{ toolName, inputText, resultText, rawStart: item.rawStart, rawEnd: item.rawEnd }}
      index={index}
    />
  );
}

function ConversationMarker({
  item,
}: {
  item: Extract<TimelineItem, { kind: 'conversation_marker' }>;
}): React.ReactNode {
  return (
    <div className="conversation-marker">
      <span className="conversation-marker-line" />
      <span className="conversation-marker-label">{escapeHtml(item.label)}</span>
      <span className="conversation-marker-line" />
    </div>
  );
}

// === 时间线卡片路由 ===

function normalizeTimelineMessage(message: Message): TimelineItem {
  return {
    kind: 'message',
    id: message.message_id,
    role: message.role,
    content: message.content,
    createdAt: message.created_at,
    metadata: message.metadata ?? {},
  };
}

function TimelineCard({ item, index }: { item: TimelineItem; index: number }): React.ReactNode {
  if (item.kind === 'streaming') {
    return <StreamingCard content={item.content} index={index} />;
  }

  if (item.kind === 'conversation_marker') {
    return <ConversationMarker item={item} />;
  }

  if (item.kind === 'aggregated_text') {
    return <AggregatedTextCard item={item} index={index} />;
  }

  if (item.kind === 'aggregated_tool') {
    return <AggregatedToolCard item={item} index={index} />;
  }

  if (item.kind === 'message') {
    const isUser = item.role === 'user';
    // Assistant 消息的 content 通常由后端将 reasoning + text 串联合并保存，
    // 单独显示会与上方 aggregated_text（推理过程/最终回复）重复。
    // 这里只展示 content 的长度与简短摘要，详细文本已在独立的思考/回复卡片中呈现。
    const trimmed = (item.content || '').trim();
    const summaryPreview = trimmed.length > 80 ? `${trimmed.slice(0, 80)}…` : trimmed;

    return (
      <EventCard
        title={isUser ? '用户消息' : 'Assistant 消息'}
        kind={isUser ? 'system' : 'response'}
        tone={isUser ? 'running' : 'done'}
        time={displayTime(item.createdAt)}
        summary={isUser ? item.content : summaryPreview || '（无内容）'}
        content={isUser ? item.content : trimmed}
        raw={{
          kind: item.kind,
          id: item.id,
          role: item.role,
          content: item.content,
          createdAt: item.createdAt,
          metadata: item.metadata,
        }}
        index={index}
      />
    );
  }

  const payload = item.payload;
  const normalized = normalizeTraceData(item.eventType, payload);
  const tone = normalized.kind === 'error' ? 'danger' : normalized.kind === 'response' ? 'done' : 'running';

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

// === 事件聚合 ===

function extractEventInfo(event: TraceEvent): {
  eventType: string;
  payload: Record<string, unknown>;
  timestamp: string | null;
  eventId: string | null;
} {
  // 后端 DTO 格式可能将真实 payload 嵌套在 raw.payload 中
  const raw = (event as Record<string, unknown>).raw as Record<string, unknown> | undefined;
  const directPayload = (event.payload ?? {}) as Record<string, unknown>;
  // 如果顶层 payload 为空但 raw.payload 存在，则使用 raw.payload
  const hasDirectPayload = Object.keys(directPayload).length > 0;
  const innerPayload = (raw?.payload ?? {}) as Record<string, unknown>;
  const hasInnerPayload = Object.keys(innerPayload).length > 0;
  const effectivePayload = hasDirectPayload ? directPayload : (hasInnerPayload ? innerPayload : {});
  return {
    eventType: event.type,
    payload: effectivePayload as Record<string, unknown>,
    timestamp: event.timestamp ?? null,
    eventId: event.event_id ?? null,
  };
}

/** 按顺序处理事件，将 text_start/delta/end 合并为 aggregated_text，将 tool_call_start/end 合并为 aggregated_tool。
 *  当 LLM 没有输出内容时，回退显示关键基础设施事件而非空白。 */
function aggregateConversationEvents(events: TraceEvent[], convId: string): TimelineItem[] {
  const items: TimelineItem[] = [];
  const seenIds = new Set<string>();
  let hasOutputContent = false;

  // 文本流合并状态
  let textBuf: string[] = [];
  let textPhase = '';
  let textTs: string | null = null;
  let textEventCount = 0;
  let textRawEvents: Array<{ type: string; payload: Record<string, unknown> }> = [];
  let textFirstId: string | null = null;

  // 工具调用合并状态
  const pendingToolCalls = new Map<
    string,
    { payload: Record<string, unknown>; timestamp: string | null; eventId: string | null }
  >();

  // 回退信息
  let llmModel = '';
  let agentEndTs: string | null = null;

  function flushTextBuf() {
    if (textBuf.length === 0) return;
    const allText = textBuf.join('');
    hasOutputContent = true;
    items.push({
      kind: 'aggregated_text',
      id: `agg-text-${convId}-${textFirstId || textTs || items.length}`,
      text: allText,
      phase: textPhase,
      timestamp: textTs,
      eventCount: textEventCount,
      rawEvents: textRawEvents,
    });
    textBuf = [];
    textPhase = '';
    textTs = null;
    textEventCount = 0;
    textRawEvents = [];
    textFirstId = null;
  }

  function addTraceItem(
    eventType: string,
    payload: Record<string, unknown>,
    timestamp: string | null,
    eventId: string | null,
  ) {
    const uniqueKey = eventId ? `trace-${eventId}` : `trace-${eventType}-${timestamp ?? items.length}`;
    if (eventId && seenIds.has(uniqueKey)) return;
    if (eventId) seenIds.add(uniqueKey);
    items.push({ kind: 'trace', id: uniqueKey, eventType, payload, timestamp });
  }

  for (const event of events) {
    const { eventType, payload, timestamp, eventId } = extractEventInfo(event);
    const type = eventType.toLowerCase();

    // --- 记录回退信息 ---
    if (type === 'llm_request') {
      llmModel = getOptionalString(payload, 'model') || llmModel;
    }
    if (type === 'agent_end') {
      agentEndTs = timestamp;
    }

    // --- 聚合：文本流 ---
    if (type === 'text_start') {
      // text_start 只是流的开始信号，不要立即 push 任何内容。
      // 实际 phase 和内容由后续 text_delta 决定。
      flushTextBuf();
      const initialText = getOptionalString(payload, 'text');
      textBuf = initialText ? [initialText] : [];
      // 如果 payload 显式声明了 phase/kind 则使用它，否则保留空（由首个 text_delta 决定）
      const startPhase = getOptionalString(payload, 'phase') || getOptionalString(payload, 'kind');
      textPhase = startPhase === 'reasoning' ? 'reasoning' : (startPhase ? 'text' : '');
      textTs = timestamp;
      textEventCount = initialText ? 1 : 0;
      textRawEvents = initialText ? [{ type: eventType, payload }] : [];
      textFirstId = eventId;
      continue;
    }
    if (type === 'text_delta') {
      const deltaKind = getOptionalString(payload, 'kind') || 'text';
      const deltaPhase = deltaKind === 'reasoning' ? 'reasoning' : 'text';
      const deltaText = getOptionalString(payload, 'text');
      // phase 切换时先 flush 当前 buffer
      if (textPhase && textPhase !== deltaPhase) {
        flushTextBuf();
      }
      // 空文本（如 reasoning_start/end 标记）不参与聚合，仅作为 phase 边界
      if (deltaText) {
        textBuf.push(deltaText);
        textEventCount++;
        textRawEvents.push({ type: eventType, payload });
      }
      textPhase = deltaPhase;
      if (!textTs) textTs = timestamp;
      if (!textFirstId) textFirstId = eventId;
      continue;
    }
    if (type === 'text_end') {
      // 后端 text_end 携带的 text 与已累加的 text_delta 内容完全相同
      // （final_text = "".join(collected_text_parts)），不再 push，避免重复。
      flushTextBuf();
      continue;
    }

    // --- 聚合：工具调用 ---
    if (type === 'tool_call_start') {
      flushTextBuf();
      const toolName = getOptionalString(payload, 'tool_name');
      pendingToolCalls.set(toolName, { payload, timestamp, eventId });
      continue;
    }
    if (type === 'tool_call_end') {
      flushTextBuf();
      const toolName = getOptionalString(payload, 'tool_name');
      const pending = pendingToolCalls.get(toolName);
      if (pending) {
        pendingToolCalls.delete(toolName);
        hasOutputContent = true;
        const startPayload = pending.payload;
        const resultText = getOptionalString(payload, 'result');
        const inputMsg = getOptionalString(startPayload, 'message') || getOptionalString(startPayload, 'content');
        items.push({
          kind: 'aggregated_tool',
          id: `agg-tool-${convId}-${pending.eventId || pending.timestamp || items.length}`,
          toolName,
          inputText: inputMsg,
          resultText,
          timestamp: pending.timestamp,
          rawStart: startPayload,
          rawEnd: payload,
        });
      } else {
        addTraceItem(eventType, payload, timestamp, eventId);
      }
      continue;
    }

    // --- 系统提醒：保留 ---
    if (type === 'system_reminder_injected') {
      flushTextBuf();
      hasOutputContent = true;
      addTraceItem(eventType, payload, timestamp, eventId);
      continue;
    }

    // --- 错误事件：始终显示 ---
    if (type === 'error' || type === 'job_failed' || type === 'job_cancelled') {
      flushTextBuf();
      hasOutputContent = true;
      addTraceItem(eventType, payload, timestamp, eventId);
      continue;
    }

    // --- 跳过纯噪音事件 ---
    if (
      type === 'job_completed' ||
      type === 'status_change' ||
      type === 'job_created' ||
      type === 'job_started' ||
      type === 'agent_start' ||
      type === 'agent_step' ||
      type === 'llm_request' ||
      type === 'agent_end'
    ) {
      continue;
    }

    if (type === 'message_created') {
      continue;
    }

    // --- 其他非标事件 ---
    flushTextBuf();
    hasOutputContent = true;
    addTraceItem(eventType, payload, timestamp, eventId);
  }

  // 处理未闭合的文本块和工具调用
  flushTextBuf();
  for (const [, pending] of pendingToolCalls) {
    addTraceItem('tool_call_start', pending.payload, pending.timestamp, pending.eventId);
  }

  // --- 回退：当 LLM 没有输出任何内容时 ---
  if (!hasOutputContent && items.length === 0) {
    const modelInfo = llmModel ? `模型: ${llmModel}` : '';
    items.push({
      kind: 'trace',
      id: `fallback-agent-end-${convId}-${agentEndTs || items.length}`,
      eventType: 'llm_empty_response',
      payload: { message: modelInfo || 'LLM 返回了空响应', model: llmModel },
      timestamp: agentEndTs,
    });
  }

  return items;
}

function buildTraceTimelineItems(conversations: ConversationView[]): TimelineItem[] {
  const items: TimelineItem[] = [];

  for (let ci = 0; ci < conversations.length; ci++) {
    const conv = conversations[ci];

    // --- 对话分隔标记 ---
    items.push({
      kind: 'conversation_marker',
      id: `${conv.conversationId}-marker`,
      label: `第 ${ci + 1} 轮对话`,
    });

    // --- 用户消息卡片 ---
    if (conv.userMessage) {
      items.push(normalizeTimelineMessage(conv.userMessage));
    }

    // --- 流式文本 ---
    if (conv.streamingTextActive && conv.streamingText != null) {
      items.push({
        kind: 'streaming',
        id: `${conv.conversationId}-streaming-${Date.now()}`,
        content: conv.streamingText,
      });
    }

    // --- 聚合后的 trace 事件（不再跳过 aggregated_text，让 reasoning 与最终回复分别独立显示） ---
    const aggregated = aggregateConversationEvents(conv.events, conv.conversationId);
    for (const item of aggregated) {
      items.push(item);
    }

    // --- Assistant 消息不再单独渲染：后端 AIMessage.content = reasoning + text 串接
    //     与上方 aggregated_text 卡片（推理过程 / 最终回复）完全重复，省略避免视觉冗余。 ---
  }

  return items;
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
    <section className="chat-stream" data-expand-details={String(expandDetails)}>
      {timelineItems.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state-title">对话区</div>
          <div>输入消息后，这里会显示完整的会话卡片、回复和 trace 细节。</div>
        </div>
      ) : (
        timelineItems.map((item, index) => <TimelineCard key={item.id} item={item} index={index} />)
      )}
      <div className="event-stream-bottom-spacer" />
    </section>
  );
}
