import React from 'react';
import { useAppState } from '../hooks';
import type { ConversationView, Message, TraceEvent } from '../types';
import { escapeHtml, formatTime } from '../utils/format';
import { renderMarkdown } from '../utils/markdown';

function displayTime(value: unknown): string {
  return formatTime(value) || 'now';
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
      throw new Error('assistant content 缺少 thought/response 字段');
    }

    return { thought, response, rawText: trimmed, rawJson: { thought, response } };
  } catch {
    return { thought: '', response: trimmed, rawText: trimmed, rawJson: null };
  }
}

type TimelineItem =
  | { kind: 'message'; id: string; role: Message['role']; content: string; createdAt: string | null; metadata: Record<string, unknown> }
  | { kind: 'trace'; id: string; eventType: string; payload: Record<string, unknown>; timestamp: string | null };

function normalizeTraceData(eventType: string, payload: Record<string, unknown>): {
  kind: 'thought' | 'tool_call' | 'tool_result' | 'response' | 'system' | 'error';
  title: string;
  summary: string;
  content: string;
} {
  const type = String(eventType ?? '').toLowerCase();
  const toolName = String(payload.tool_name ?? '');
  const modelName = String(payload.model ?? '');
  const filePath = String(payload.path ?? '');
  const message = String(payload.message ?? '');
  const resultText = String(payload.result ?? '');

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
    return {
      kind: 'tool_call',
      title: `调用工具 ${toolName}`,
      summary: String(payload.phase ?? ''),
      content: message || resultText || filePath,
    };
  }

  if (type === 'tool_call_end' || type === 'file_write') {
    return {
      kind: 'tool_result',
      title: type === 'tool_call_end' ? `工具结果 ${toolName}` : `文件写入 ${filePath || 'unknown path'}`,
      summary: type === 'tool_call_end' ? '工具执行完成' : '文件已写入',
      content: resultText || message || filePath,
    };
  }

  if (type === 'agent_end') {
    const finalText = String(payload.final_text ?? '').trim();
    return {
      kind: 'response',
      title: '最终响应',
      summary: `长度: ${String(payload.response_length)}`,
      content: finalText,
    };
  }

  if (type === 'error' || type === 'job_failed' || type === 'job_cancelled') {
    return {
      kind: 'error',
      title: '执行异常',
      summary: String(payload.error ?? ''),
      content: String(payload.stack ?? payload.detail ?? payload.message ?? payload.error ?? ''),
    };
  }

  return {
    kind: 'system',
    title: `事件 ${eventType}`,
    summary: [toolName ? `工具: ${toolName}` : '', modelName ? `模型: ${modelName}` : ''].filter(Boolean).join(' · '),
    content: message || resultText || filePath,
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
  const payload = event.data ?? {};
  const normalized = normalizeTraceData(event.event_type, payload);
  const tone = normalized.kind === 'error' ? 'danger' : normalized.kind === 'response' ? 'done' : 'running';
  const content = normalized.content.trim();
  const summary = normalized.summary.trim();
  const [open, setOpen] = React.useState(false);
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

function TimelineCard({ item, index }: { item: TimelineItem; index: number }): React.ReactNode {
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

export default function ChatPanel({ conversations, expandDetails }: ChatPanelProps) {
  void useAppState;

  const timelineItems = React.useMemo<TimelineItem[]>(() => {
    const items: TimelineItem[] = [];
    for (const conversation of conversations) {
      if (conversation.userMessage) {
        items.push(normalizeTimelineMessage(conversation.userMessage));
      }

      for (const assistantMessage of conversation.assistantMessages) {
        items.push(normalizeTimelineMessage(assistantMessage));
      }

      for (const event of conversation.events) {
        items.push({
          kind: 'trace',
          id: `${event.event_type}-${event.timestamp ?? items.length}`,
          eventType: event.event_type,
          payload: event.data,
          timestamp: event.timestamp,
        });
      }
    }
    return items;
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
      <div className="event-stream-bottom-spacer" />
    </section>
  );
}