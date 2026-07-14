import React from 'react';
import { useAppState } from '../hooks.tsx';
import type { ConversationView, Message, TraceEvent } from '../types';
import { escapeHtml, formatTime } from '../utils/format';
import { renderMarkdown } from '../utils/markdown';
import { formatLocalLogBlock } from '../vscode';

function displayTime(value: unknown): string {
  return formatTime(value) || 'now';
}

function getRequiredString(payload: Record<string, unknown>, key: string, eventType: string): string {
  const value = payload[key];
  if (typeof value !== 'string' || value.trim() === '') {
    const detail = JSON.stringify(payload, null, 2);
    console.error(formatLocalLogBlock(`事件结构异常 ${eventType}`, detail));
    throw new Error(`事件 ${eventType} 缺少必需字段 ${key}\n完整结构:\n${detail}`);
  }
  return value;
}

function getOptionalString(payload: Record<string, unknown>, key: string): string {
  const value = payload[key];
  return typeof value === 'string' ? value : '';
}

function normalizeAssistantPayload(content: string): { thought: string; response: string; rawText: string; rawJson: { thought?: string; response?: string } | null } {
  const trimmed = String(content ?? '').trim();
  if (!trimmed) {
    return { thought: '', response: '', rawText: '', rawJson: null };
  }

  try {
    const parsed = JSON.parse(trimmed) as { thought?: unknown; response?: unknown };
    const thought = String(parsed?.thought ?? '').trim();
    const response = String(parsed?.response ?? '').trim();

    if (!thought && !response) {
      return { thought: '', response: trimmed, rawText: trimmed, rawJson: null };
    }

    return { thought, response, rawText: trimmed, rawJson: { thought, response } };
  } catch {
    return { thought: '', response: trimmed, rawText: trimmed, rawJson: null };
  }
}

type TimelineItem =
  | { kind: 'message'; id: string; role: Message['role']; content: string; createdAt: string | null; metadata: Record<string, unknown> }
  | { kind: 'trace'; id: string; eventType: string; payload: Record<string, unknown>; timestamp: string | null }
  | { kind: 'streaming'; id: string; content: string }
  | { kind: 'aggregated_text'; id: string; text: string; phase: string; timestamp: string | null; eventCount: number; rawEvents: Array<{ type: string; payload: Record<string, unknown> }> }
  | { kind: 'aggregated_tool'; id: string; toolName: string; inputText: string; resultText: string; timestamp: string | null; rawStart: Record<string, unknown>; rawEnd: Record<string, unknown> }
  | { kind: 'conversation_marker'; id: string; label: string };

function normalizeTraceData(eventType: string, payload: Record<string, unknown>): {
  kind: 'thought' | 'tool_call' | 'tool_result' | 'response' | 'system' | 'error';
  title: string;
  summary: string;
  content: string;
} {
  const type = String(eventType ?? '').toLowerCase();
  const agentEndPayload = payload.payload && typeof payload.payload === 'object' ? (payload.payload as Record<string, unknown>) : payload;
  const message = getOptionalString(payload, 'message');
  const resultText = getOptionalString(payload, 'result');
  const toolName = getOptionalString(payload, 'tool_name');
  const modelName = getOptionalString(payload, 'model');
  const errorText = String(payload.error ?? payload.message ?? payload.detail ?? '').trim();

  if (type === 'agent_start' || type === 'agent_step' || type === 'llm_request' || type === 'model_call') {
    if (type === 'model_call') {
      throw new Error('已废弃的事件类型 model_call 不应再出现');
    }

    return {
      kind: 'thought',
      title: type === 'agent_start' ? 'Agent 思考' : 'Agent 思考中',
      summary: String(payload.phase ?? ''),
      content: message || resultText,
    };
  }

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

  if (type === 'agent_end') {
    const finalTextValue = getRequiredString(agentEndPayload, 'final_text', type);
    return {
      kind: 'response',
      title: '最终响应',
      summary: `长度: ${finalTextValue.trim().length}`,
      content: finalTextValue.trim(),
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

  // aggregated_text: 合并后的文本流（text_start + text_delta* + text_end）
  if (type === 'aggregated_text') {
    const text = getOptionalString(payload, 'text');
    const phase = getOptionalString(payload, 'phase') || 'text';
    const eventCount = typeof payload.event_count === 'number' ? payload.event_count : 0;
    const isReasoning = phase === 'reasoning';
    return {
      kind: isReasoning ? 'thought' : 'response',
      title: isReasoning ? '推理过程' : 'Agent 思考过程',
      summary: eventCount > 0 ? `（${eventCount} 个文本事件已合并）` : '',
      content: text,
    };
  }

  // aggregated_tool: 合并后的工具调用（tool_call_start + tool_call_end）
  if (type === 'aggregated_tool') {
    const toolName = getOptionalString(payload, 'tool_name');
    const resultText = getOptionalString(payload, 'result');
    const inputMsg = getOptionalString(payload, 'input_message');
    return {
      kind: 'tool_call',
      title: `调用工具 ${toolName}`,
      summary: inputMsg || `工具 ${toolName} 调用详情`,
      content: resultText || inputMsg || '无结果',
    };
  }

  if (type === 'text_start' || type === 'text_delta' || type === 'text_end') {
    // 这些事件应该已被 aggregated_text 取代；如果出现则静默跳过
    return {
      kind: 'thought',
      title: type === 'text_start' ? '文本开始' : type === 'text_delta' ? '文本流' : '文本结束',
      summary: '',
      content: '',
    };
  }

  if (type === 'message_created') {
    const contentText = getOptionalString(payload, 'content');
    return {
      kind: 'system',
      title: '用户消息',
      summary: contentText.slice(0, 100) || '消息已创建',
      content: contentText || '用户消息已记录',
    };
  }

  if (type === 'job_created') {
    const contentText = getOptionalString(payload, 'content') || getOptionalString(payload, 'message');
    return {
      kind: 'system',
      title: '任务已创建',
      summary: contentText.slice(0, 100) || '任务已创建，准备执行',
      content: contentText || '任务已创建',
    };
  }

  if (type === 'job_started') {
    return {
      kind: 'system',
      title: '任务已开始',
      summary: '任务已开始执行',
      content: message || '正在执行...',
    };
  }

  if (type === 'job_completed') {
    return {
      kind: 'system',
      title: '任务已完成',
      summary: '任务已成功完成',
      content: resultText || message || '任务完成',
    };
  }

  if (type === 'status_change') {
    const statusText = getOptionalString(payload, 'status');
    const reason = getOptionalString(payload, 'reason');
    return {
      kind: 'system',
      title: '状态变更',
      summary: statusText || '状态已更新',
      content: reason || statusText || '状态已变更',
    };
  }

  return {
    kind: 'system',
    title: `事件 ${eventType}`,
    summary: [toolName ? `工具: ${toolName}` : '', modelName ? `模型: ${modelName}` : ''].filter(Boolean).join(' · '),
    content: message || resultText || errorText,
  };
}

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
        <div className="event-card-summary event-card-summary-collapsed">{escapeHtml(content || summary || '（无可读内容）')}</div>
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

function TraceLine({ event, index }: { event: TraceEvent; index: number }): React.ReactNode {
  const payload = event.payload as unknown as Record<string, unknown>;
  const normalized = normalizeTraceData(event.type, payload);
  const tone = normalized.kind === 'error' ? 'danger' : normalized.kind === 'response' ? 'done' : 'running';
  const content = normalized.content.trim();
  const summary = normalized.summary.trim();
  const collapsedText = content || summary || '（无可读内容）';

  return (
    <EventCard
      title={normalized.title}
      kind={normalized.kind}
      tone={tone}
      time={displayTime(event.timestamp)}
      summary={summary}
      content={collapsedText}
      raw={payload}
      index={index}
    />
  );
}

function normalizeTimelineMessage(message: Message): TimelineItem {
  return {
    kind: 'message',
    id: message.message_id,
    role: message.role,
    content: message.content,
    createdAt: message.created_at,
    metadata: message.metadata,
  };
}

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

function AggregatedTextCard({ item, index }: { item: Extract<TimelineItem, { kind: 'aggregated_text' }>; index: number }): React.ReactNode {
  const text = item.text.trim();
  const collapsedPreview = text.slice(0, 200) + (text.length > 200 ? '…' : '');
  return (
    <EventCard
      title="Agent 思考过程"
      kind="thought"
      tone="running"
      time={displayTime(item.timestamp)}
      summary={`${item.eventCount} 个文本事件已合并` + (item.phase ? ` · 阶段: ${item.phase}` : '')}
      content={text || '（空）'}
      raw={{ aggregated: true, eventCount: item.eventCount, phase: item.phase, text, rawEvents: item.rawEvents }}
      index={index}
    />
  );
}

function AggregatedToolCard({ item, index }: { item: Extract<TimelineItem, { kind: 'aggregated_tool' }>; index: number }): React.ReactNode {
  const { toolName, inputText, resultText, timestamp } = item;
  const summaryParts: string[] = [toolName];
  if (inputText) summaryParts.push(inputText.slice(0, 80));
  const content = [
    inputText ? `**输入参数**\n\`\`\`\n${inputText}\n\`\`\`` : '',
    resultText ? `**执行结果**\n\`\`\`\n${resultText}\n\`\`\`` : '',
  ].filter(Boolean).join('\n\n') || '（无详情）';

  return (
    <EventCard
      title={`🔧 ${toolName}`}
      kind="tool_call"
      tone="done"
      time={displayTime(timestamp)}
      summary={summaryParts.join(' · ')}
      content={content}
      raw={{ toolName, inputText, resultText, rawStart: item.rawStart, rawEnd: item.rawEnd }}
      index={index}
    />
  );
}

function ConversationMarker({ item }: { item: Extract<TimelineItem, { kind: 'conversation_marker' }> }): React.ReactNode {
  return (
    <div className="conversation-marker">
      <span className="conversation-marker-line" />
      <span className="conversation-marker-label">{escapeHtml(item.label)}</span>
      <span className="conversation-marker-line" />
    </div>
  );
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
    const assistantParsed = item.role === 'assistant' ? normalizeAssistantPayload(item.content) : null;
    const thought = assistantParsed?.thought?.trim() || '';
    const response = assistantParsed?.response?.trim() || item.content.trim() || '';
    const assistantBody = thought && response ? `**思考**\n${thought}\n\n${response}` : (thought || response);

    return (
      <EventCard
        title={isUser ? '用户消息' : 'Assistant 消息'}
        kind={isUser ? 'system' : 'response'}
        tone={isUser ? 'running' : 'done'}
        time={displayTime(item.createdAt)}
        summary={isUser ? item.content : (response || thought || '（无可读内容）')}
        content={isUser ? item.content : assistantBody}
        raw={{ kind: item.kind, id: item.id, role: item.role, content: item.content, createdAt: item.createdAt, metadata: item.metadata }}
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

interface ChatPanelProps {
  conversations: ConversationView[];
  expandDetails: boolean;
}

function normalizeTraceEventKind(eventType: string): TimelineItem['kind'] {
  const type = String(eventType ?? '').toLowerCase();
  if (type === 'error') {
    return 'error';
  }
  if (type === 'agent_end') {
    return 'response';
  }
  if (type === 'agent_start' || type === 'llm_request') {
    return 'thought';
  }
  if (type === 'tool_call_start') {
    return 'tool_call';
  }
  if (type === 'tool_call_end') {
    return 'tool_result';
  }
  return 'system';
}

// --- 事件聚合工具函数 ---

function extractEventInfo(event: TraceEvent | Record<string, unknown>): {
  eventType: string;
  payload: Record<string, unknown>;
  timestamp: string | null;
  eventId: string | null;
} {
  const legacy = event as { event_type?: string; data?: Record<string, unknown>; timestamp?: string | null };
  return {
    eventType: 'type' in event ? String(event.type) : (legacy.event_type ?? 'unknown'),
    payload: ('payload' in event ? event.payload : (legacy.data ?? {})) as Record<string, unknown>,
    timestamp: 'timestamp' in event ? (event.timestamp as string | null) : legacy.timestamp ?? null,
    eventId: 'event_id' in event ? (event.event_id as string | null) : null,
  };
}

/** 按顺序处理事件，将 text_start/delta/end 合并为 aggregated_text，将 tool_call_start/end 合并为 aggregated_tool。
 *  当 LLM 没有输出内容时，回退显示关键基础设施事件而非空白。 */
function aggregateConversationEvents(events: TraceEvent[], convId: string): TimelineItem[] {
  const items: TimelineItem[] = [];
  const seenIds = new Set<string>();
  let hasOutputContent = false; // 是否有聚合文本、工具调用或非噪音事件

  // 文本流合并状态
  let textBuf: string[] = [];
  let textPhase = '';
  let textTs: string | null = null;
  let textEventCount = 0;
  let textRawEvents: Array<{ type: string; payload: Record<string, unknown> }> = [];
  let textFirstId: string | null = null;

  // 工具调用合并状态：key=tool_name，value=待匹配的 start 信息
  const pendingToolCalls = new Map<string, { payload: Record<string, unknown>; timestamp: string | null; eventId: string | null }>();

  // 回退信息：LLM 调用摘要
  let llmModel = '';
  let agentEndTs: string | null = null;

  function flushTextBuf() {
    if (textBuf.length === 0) return;
    const allText = textBuf.join('');
    hasOutputContent = true;
    items.push({
      kind: 'aggregated_text',
      id: `agg-text-${textFirstId || textTs || items.length}`,
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

  function addTraceItem(eventType: string, payload: Record<string, unknown>, timestamp: string | null, eventId: string | null) {
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
      flushTextBuf();
      textBuf = [getOptionalString(payload, 'text')];
      textPhase = getOptionalString(payload, 'phase') || getOptionalString(payload, 'kind') || 'text';
      textTs = timestamp;
      textEventCount = 1;
      textRawEvents = [{ type: eventType, payload }];
      textFirstId = eventId;
      continue;
    }
    if (type === 'text_delta') {
      const deltaKind = getOptionalString(payload, 'kind') || 'text';
      const deltaPhase = deltaKind === 'reasoning' ? 'reasoning' : 'text';
      // phase 切换时先 flush 当前 buffer
      if (textPhase && textPhase !== deltaPhase) {
        flushTextBuf();
      }
      textBuf.push(getOptionalString(payload, 'text'));
      textPhase = deltaPhase;
      textEventCount++;
      textRawEvents.push({ type: eventType, payload });
      if (!textTs) textTs = timestamp;
      if (!textFirstId) textFirstId = eventId;
      continue;
    }
    if (type === 'text_end') {
      textBuf.push(getOptionalString(payload, 'text'));
      textEventCount++;
      textRawEvents.push({ type: eventType, payload });
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
          id: `agg-tool-${pending.eventId || pending.timestamp || items.length}`,
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

    // --- 错误事件：始终显示 ---
    if (type === 'error' || type === 'job_failed' || type === 'job_cancelled') {
      flushTextBuf();
      hasOutputContent = true;
      addTraceItem(eventType, payload, timestamp, eventId);
      continue;
    }

    // --- 跳过纯噪音事件 ---
    if (type === 'job_completed' || type === 'status_change' ||
        type === 'job_created' || type === 'job_started' ||
        type === 'agent_start' || type === 'agent_step' ||
        type === 'llm_request' || type === 'agent_end') {
      continue;
    }

    // message_created 已通过用户消息卡片展示，跳过
    if (type === 'message_created') {
      continue;
    }

    // --- 其他非标事件：也算内容 ---
    flushTextBuf();
    hasOutputContent = true;
    addTraceItem(eventType, payload, timestamp, eventId);
  }

  // 处理未闭合的文本块和工具调用
  flushTextBuf();
  for (const [, pending] of pendingToolCalls) {
    addTraceItem('tool_call_start', pending.payload, pending.timestamp, pending.eventId);
  }

  // --- 回退：当 LLM 没有输出任何内容时，插入摘要卡片 ---
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
    const convLabel = `第 ${ci + 1} 轮对话`;
    items.push({
      kind: 'conversation_marker',
      id: `${conv.conversationId}-marker`,
      label: convLabel,
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

    // --- 聚合后的 trace 事件（跳过 aggregated_text，流式文本已包含在 Assistant 消息中） ---
    const aggregated = aggregateConversationEvents(conv.events, conv.conversationId);
    for (const item of aggregated) {
      if (item.kind === 'aggregated_text') continue;
      items.push(item);
    }

    // --- Assistant 消息卡片（跳过空内容） ---
    for (const msg of conv.assistantMessages) {
      if (!msg.content || msg.content.trim() === '') continue;
      items.push(normalizeTimelineMessage(msg));
    }
  }

  return items;
}

export default function ChatPanel({ conversations, expandDetails }: ChatPanelProps) {
  void useAppState;

  const timelineItems = React.useMemo<TimelineItem[]>(() => {
    return buildTraceTimelineItems(conversations);
  }, [conversations]);

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
    </section>
  );
}
