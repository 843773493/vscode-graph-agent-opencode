import React from 'react';
import type { Message, TraceEvent } from '../types/backend';
import { escapeHtml, formatTime } from '../utils/format';
import { renderMarkdown } from '../utils/markdown';

function displayTime(value: unknown): string {
  return formatTime(value) || 'now';
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

function TimelineCard({ item, index }: { item: { kind: 'message' | 'trace'; id: string; role?: Message['role']; content: string; createdAt?: string | null; metadata?: Record<string, unknown>; eventType?: string; payload?: Record<string, unknown>; timestamp?: string | null }; index: number }): React.ReactNode {
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

  const payload = item.payload ?? {};
  return (
    <EventCard
      title={`事件 ${item.eventType ?? 'trace'}`}
      tone="running"
      time={displayTime(item.timestamp)}
      summary={String(payload.message ?? payload.result ?? '')}
      content={String(payload.message ?? payload.result ?? '')}
      raw={payload}
    />
  );
}

export default function ChatPanel({ conversations, expandDetails }: { conversations: Array<{ conversationId: string; userMessage: Message | null; assistantMessages: Message[]; events: TraceEvent[] }> ; expandDetails: boolean }) {
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
            {expandDetails && conversation.events.map((event, eventIndex) => (
              <TimelineCard key={event.event_id} item={{ kind: 'trace', id: event.event_id, eventType: event.type, content: '', payload: event.payload as unknown as Record<string, unknown>, timestamp: event.timestamp }} index={eventIndex} />
            ))}
          </section>
        ))
      )}
    </div>
  );
}
