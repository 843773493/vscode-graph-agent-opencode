import React from 'react';
import type { PendingTurn, TraceEvent, Message } from '../types';
import { renderMarkdown } from '../utils/markdown';
import { formatTime, escapeHtml } from '../utils/format';

function isErrorTrace(event: TraceEvent): boolean {
  const type = String(event.event_type ?? '').toLowerCase();
  return type === 'error' || type === 'job_failed' || type === 'job_cancelled';
}

function renderTraceGroupTitle(eventType: string, payload: Record<string, unknown>): string {
  const type = eventType?.toLowerCase();
  if (type === 'agent_start') return `开始处理：${payload?.message ? String(payload.message) : '启动 agent'}`;
  if (type === 'agent_step') return payload?.phase ? `阶段：${String(payload.phase)}` : '执行中';
  if (type === 'tool_call_start') return `调用工具：${String(payload?.tool_name || 'unknown_tool')}`;
  if (type === 'tool_call_end') return `工具完成：${String(payload?.tool_name || 'unknown_tool')}`;
  if (type === 'file_write') return `文件写入：${String(payload?.path || payload?.file_path || 'unknown path')}`;
  if (type === 'llm_request' || type === 'model_call') return `模型调用：${String(payload?.model || 'unknown model')}`;
  if (type === 'agent_end') return `结束处理：${String(payload?.final_message_count ?? 0)} 条消息`;
  if (type === 'error') return `错误：${String(payload?.error || 'unknown error')}`;
  return `事件：${eventType}`;
}

function renderOutputStream(label: string, events: TraceEvent[]): React.ReactNode {
  if (!events.length) return null;
  return (
    <section className="output-stream">
      <h3 className="output-stream-title">{escapeHtml(label)}</h3>
      <div className="output-stream-body">
        {events.map((ev, index) => (
          <article key={`${ev.event_type}-${ev.timestamp ?? index}`} className="output-event-card">
            <div className="editor-head">
              <span>{escapeHtml(renderTraceGroupTitle(ev.event_type, ev.data))}</span>
              <span className="badge neutral">{escapeHtml(formatTime(ev.timestamp) || 'now')}</span>
            </div>
            <div className="editor-body">
              <pre>{escapeHtml(JSON.stringify(ev.data, null, 2))}</pre>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function TracePanel({ events }: { events: TraceEvent[] }): React.ReactNode {
  const stdout = events.filter(event => !isErrorTrace(event));
  const stderr = events.filter(event => isErrorTrace(event));
  if (!events.length) return null;

  return (
    <details className="request-container output-container" open>
      <summary className="title">
        <div className="request-main">
          <span className="request-chevron">▼</span>
          <span className="request-title">Output</span>
        </div>
        <div className="request-stats">
          <span className="badge neutral">{String(events.length)} events</span>
        </div>
      </summary>
      <div className="request-details">
        {renderOutputStream('stdout', stdout)}
        {renderOutputStream('stderr', stderr)}
      </div>
    </details>
  );
}

function MessageActions({ onAction }: { onAction: (action: string) => void }): React.ReactNode {
  return <div className="message-actions" />;
}

function compileTurn(
  userMessage: Message | null,
  assistantMessages: Message[],
  events: TraceEvent[],
  expandDetails: boolean,
  onMessageAction: (action: string, messageId: string) => void,
): React.ReactNode {
  return (
    <div className="request-container">
      {userMessage && (
        <section className="chat-message user">
          <div className="chat-message-head">
            <span>You</span>
            <span>{escapeHtml(formatTime(userMessage.created_at) || '')}</span>
          </div>
          <div className="chat-message-body reply" dangerouslySetInnerHTML={{ __html: renderMarkdown(userMessage.content || '') }} />
        </section>
      )}
      <section>
        <div className="turn-section-body">
          {assistantMessages.map((msg, index) => (
            <div key={`${msg.message_id}_${index}`} className="chat-message assistant">
              <div className="chat-message-head">
                <span>{index === 0 ? 'Assistant' : `Assistant #${index + 1}`}</span>
                <span>{escapeHtml(formatTime(msg.created_at) || '')}</span>
                <MessageActions onAction={action => onMessageAction(action, msg.message_id)} />
              </div>
              <div className="chat-message-body reply" dangerouslySetInnerHTML={{ __html: renderMarkdown(msg.content || '') }} />
            </div>
          ))}
          {!assistantMessages.length && (
            <div className="chat-message assistant">
              <div className="chat-message-head">
                <span>Assistant</span>
                <span>running</span>
              </div>
              <div className="chat-message-body reply">正在思考并调用工具...</div>
            </div>
          )}
        </div>
      </section>
      {expandDetails && events.length > 0 && <TracePanel events={events} />}
    </div>
  );
}

interface ChatPanelProps {
  turns: PendingTurn[];
  expandDetails: boolean;
}

export default function ChatPanel({ turns, expandDetails }: ChatPanelProps) {
  const handleMessageAction = (action: string) => {
    void action;
  };

  return (
    <section className="chat-stream">
      {turns.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state-title">会话</div>
          <div>输入内容开始聊天。</div>
        </div>
      ) : (
        turns.map(turn => (
          <React.Fragment key={turn.turnId}>
            {compileTurn(turn.userMessage, turn.assistantMessages, turn.events, expandDetails, handleMessageAction)}
          </React.Fragment>
        ))
      )}
    </section>
  );
}
