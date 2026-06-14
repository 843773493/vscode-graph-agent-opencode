import React from 'react';
import type { Message, TraceEvent } from '../types/backend';
import { escapeHtml, formatTime } from '../utils/format';
import { renderMarkdown } from '../utils/markdown';

function displayTime(value: unknown): string {
  return formatTime(value) || 'now';
}

function getTraceTime(item: { timestamp?: string | null; time?: string | null }): string {
  return item.timestamp || item.time || '';
}

function normalizeAssistantPayload(content: string): { thought: string; response: string } {
  const trimmed = String(content ?? '').trim();
  if (!trimmed) {
    return { thought: '', response: '' };
  }

  try {
    const parsed = JSON.parse(trimmed) as { thought?: unknown; response?: unknown };
    const thought = String(parsed?.thought ?? '').trim();
    const response = String(parsed?.response ?? '').trim();
    if (!thought && !response) {
      return { thought: '', response: trimmed };
    }
    return { thought, response };
  } catch {
    return { thought: '', response: trimmed };
  }
}

function EventCard({
  title,
  tone,
  time,
  summary,
  content,
  raw,
}: {
  title: string;
  tone: 'running' | 'done' | 'danger';
  time: string;
  summary: string;
  content: string;
  raw: Record<string, unknown>;
}): React.ReactNode {
  const [open, setOpen] = React.useState(false);

  return (
    <article className={`event-card tone-${tone} ${open ? 'is-open' : 'is-collapsed'}`}>
      <div className="event-card-head">
        <div className="event-card-title-row">
          <span className={`event-card-indicator event-card-indicator-${tone}`} />
          <span className="event-card-title">{escapeHtml(title)}</span>
        </div>
        <div className="event-card-head-right">
          <span className="badge neutral event-card-time">{escapeHtml(time)}</span>
          <button type="button" className="event-card-toggle" aria-expanded={open} aria-label={open ? '折叠' : '展开'} onClick={() => setOpen(prev => !prev)}>
            {open ? '−' : '+'}
          </button>
        </div>
      </div>
      {!open ? (
        <div className="event-card-summary event-card-summary-collapsed">{escapeHtml(content || summary || '（无可读内容）')}</div>
      ) : (
        <div className="event-card-body">
          {summary && <div className="event-card-summary">{escapeHtml(summary)}</div>}
          {content ? <div className="event-card-content" dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }} /> : <div className="event-card-empty">（无可读内容）</div>}
          <details className="event-card-details">
            <summary>原始数据</summary>
            <pre>{escapeHtml(JSON.stringify(raw, null, 2))}</pre>
          </details>
        </div>
      )}
    </article>
  );
}

type TimelineItem =
  | { kind: 'message'; id: string; role?: Message['role']; content: string; createdAt?: string | null; metadata?: Record<string, unknown> }
  | { kind: 'trace'; id: string; eventType?: string; payload?: Record<string, unknown>; timestamp?: string | null }
  | { kind: 'streaming'; id: string; content: string };

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
        tone={isUser ? 'running' : 'done'}
        time={displayTime(item.createdAt)}
        summary={isUser ? item.content : (response || thought || '（无可读内容）')}
        content={isUser ? item.content : assistantBody}
        raw={{ kind: item.kind, id: item.id, role: item.role, content: item.content, createdAt: item.createdAt, metadata: item.metadata }}
      />
    );
  }

  if (item.kind === 'streaming') {
    return (
      <EventCard
        title="Assistant 生成中"
        tone="running"
        time={displayTime(null)}
        summary={item.content}
        content={item.content}
        raw={{ kind: item.kind, id: item.id, content: item.content }}
      />
    );
  }

  const payload = item.payload ?? {};
  let title: string;
  let summary: string;
  let content: string;

  if (item.eventType === 'system_reminder_injected') {
    title = '系统提示';
    summary = `注入位置: ${String(payload.position ?? '')}`;
    content = String(payload.content ?? '');
  } else if (item.eventType === 'text_start') {
    title = '文本开始';
    summary = '';
    content = String(payload.text ?? '');
  } else if (item.eventType === 'text_delta') {
    title = '文本流';
    summary = '';
    content = String(payload.text ?? '');
  } else if (item.eventType === 'text_end') {
    title = '文本结束';
    summary = '';
    content = String(payload.text ?? '');
  } else {
    title = `事件 ${item.eventType ?? 'trace'}`;
    summary = String(payload.message ?? payload.result ?? '');
    content = summary;
  }

  return (
    <EventCard
      title={title}
      tone="running"
      time={displayTime(getTraceTime(item))}
      summary={summary}
      content={content}
      raw={payload}
    />
  );
}

type ConversationView = {
  conversationId: string;
  userMessage: Message | null;
  assistantMessages: Message[];
  events: TraceEvent[];
  streamingText?: string;
  streamingTextActive?: boolean;
};

export default function ChatPanel({ conversations, expandDetails }: { conversations: Array<ConversationView>; expandDetails: boolean }) {
  return (
    <div className="chat-stream">
      {conversations.length === 0 ? (
        <div className="empty-state">暂无消息，先创建会话并发送第一条消息</div>
      ) : (
        conversations.map((conversation, index) => (
          <section key={conversation.conversationId} className="event-card-container">
            <div className="conversation-title">会话 #{index + 1}</div>
            {conversation.userMessage && (
              <TimelineCard item={{ kind: 'message', id: conversation.userMessage.message_id, role: conversation.userMessage.role, content: conversation.userMessage.content, createdAt: conversation.userMessage.created_at, metadata: conversation.userMessage.metadata }} index={index} />
            )}
            {conversation.assistantMessages.map(message => (
              <TimelineCard key={message.message_id} item={{ kind: 'message', id: message.message_id, role: message.role, content: message.content, createdAt: message.created_at, metadata: message.metadata }} index={index} />
            ))}
            {conversation.streamingTextActive && conversation.streamingText && (
              <TimelineCard item={{ kind: 'streaming', id: `${conversation.conversationId}-streaming`, content: conversation.streamingText }} index={index} />
            )}
            {expandDetails && conversation.events.map((event, eventIndex) => (
              <TimelineCard key={event.event_id} item={{ kind: 'trace', id: event.event_id, eventType: event.type, content: '', payload: event.payload as unknown as Record<string, unknown>, timestamp: getTraceTime(event), }} index={eventIndex} />
            ))}
          </section>
        ))
      )}
    </div>
  );
}
