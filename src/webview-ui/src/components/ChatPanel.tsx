import React from 'react';
import type { PendingTurn, TraceEvent, Message } from '../types';
import { useAppState } from '../hooks';
import { postDebug } from '../vscode';
import { applyInlineMarkdown, renderMarkdown } from '../utils/markdown';
import { formatTime, escapeHtml, formatSnippet } from '../utils/format';

/* ---------- trace helpers ---------- */
function renderTraceGroupTitle(eventType: string, payload: Record<string, unknown>): string {
  const type = eventType?.toLowerCase();
  if (type === 'agent_start') return `开始处理：${payload?.message ? formatSnippet(payload.message, 120) : '启动 agent'}`;
  if (type === 'agent_step') return payload?.phase ? `阶段：${payload.phase}` : '执行中';
  if (type === 'tool_call_start') return `调用工具：${payload?.tool_name || 'unknown_tool'}`;
  if (type === 'tool_call_end') return `工具完成：${payload?.tool_name || 'unknown_tool'}`;
  if (type === 'file_write') return `文件写入：${payload?.path || payload?.file_path || 'unknown path'}`;
  if (type === 'llm_request' || type === 'model_call') return `模型调用：${payload?.model || 'unknown model'}`;
  if (type === 'agent_end') return `结束处理：${(payload?.final_message_count ?? 0) as number} 条消息`;
  if (type === 'error') return `错误：${payload?.error || 'unknown error'}`;
  return `事件：${eventType}`;
}

function isErrorTrace(event: TraceEvent): boolean {
  const type = String(event?.event_type ?? '').toLowerCase();
  return type === 'error' || type === 'job_failed' || type === 'job_cancelled';
}

/* ---------- OutputStream ---------- */
function renderOutputStream(label: string, events: TraceEvent[]): React.ReactNode {
  if (events.length === 0) return null;
  return (
    <section className="output-stream">
      <h3 className="output-stream-title">{escapeHtml(label)}</h3>
      <div className="output-stream-body">
        {events.map(ev => (
          <article key={`${ev.event_type}-${ev.timestamp ?? Math.random()}`} className="output-event-card">
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

/* ---------- TracePanel ---------- */
function TracePanel({ events }: { events: TraceEvent[] }): React.ReactNode {
  const stdout = events.filter(e => !isErrorTrace(e));
  const stderr = events.filter(e => isErrorTrace(e));
  if (events.length === 0) return null;

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

/* ---------- Message Actions ---------- */
const MessageActions: React.FC<{ onAction: (action: string) => void }> = ({ onAction }) => (
  <div className="message-actions">
    {(['copy', 'regenerate', 'thumbs-up', 'thumbs-down'] as const).map(action => (
      <button key={action} className="action-btn" title={action} data-action={action} onClick={() => onAction(action)}>
        {actionIcon(action)}
      </button>
    ))}
    {(['edit-retry', 'insert-editor', 'run-terminal', 'create-file', 'explain-code', 'optimize-code', 'add-comments', 'share'] as const).map(action => (
      <button key={action} className="action-btn hover-only" title={action} data-action={action} onClick={() => onAction(action)}>
        {actionIcon(action)}
      </button>
    ))}
  </div>
);

function actionIcon(action: string): React.ReactNode {
  switch (action) {
    case 'copy':
      return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="9" y="9" width="13" height="13" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>;
    case 'regenerate':
      return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="1 4 1 10 7 10"></polyline><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"></path></svg>;
    case 'thumbs-up':
      return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"></path></svg>;
    case 'thumbs-down':
      return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M10 15v1a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zm7-13h2.67A2.31 2.31 0 0 1 22 4v7a2.31 2.31 0 0 1-2.33 2H17"></path></svg>;
    case 'edit-retry':
      return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>;
    case 'insert-editor':
      return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="16 18 22 12 16 6"></polyline><line x1="2" y1="12" x2="22" y2="12"></line></svg>;
    case 'run-terminal':
      return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>;
    case 'create-file':
      return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="12" y1="18" x2="12" y2="12"></line><line x1="9" y1="15" x2="15" y2="15"></line></svg>;
    case 'explain-code':
      return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10"></circle><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"></path><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>;
    case 'optimize-code':
      return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>;
    case 'add-comments':
      return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>;
    case 'share':
      return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="18" cy="5" r="3"></circle><circle cx="6" cy="12" r="3"></circle><circle cx="18" cy="19" r="3"></circle><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"></line><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"></line></svg>;
    default:
      return null;
  }
}

/* ---------- compileTurn ---------- */
export function compileTurn(
  userMessage: Message | null,
  assistantMessages: Message[],
  events: TraceEvent[],
  expandDetails: boolean,
  onMessageAction: (action: string, messageId: string) => void,
  onCodeAction: (action: string, code: string) => void,
): React.ReactNode {
  return (
    <div className="request-container">
      {userMessage && (
        <section className="chat-message user">
          <div className="chat-message-head">
            <span>You</span>
            <span>{escapeHtml(formatTime(userMessage.created_at) || '')}</span>
          </div>
          <div
            className="chat-message-body reply"
            dangerouslySetInnerHTML={{ __html: renderMarkdown(userMessage.content || '') }}
          />
        </section>
      )}
      <section>
        <div className="turn-section-body">
          {assistantMessages.map((msg, i) => (
            <div key={msg.message_id + '_' + i} className="chat-message assistant">
              <div className="chat-message-head">
                <span>{i === 0 ? 'Assistant' : `Assistant #${i + 1}`}</span>
                <span>{escapeHtml(formatTime(msg.created_at) || '')}</span>
                <MessageActions onAction={action => onMessageAction(action, msg.message_id)} />
              </div>
              <div
                className="chat-message-body reply"
                dangerouslySetInnerHTML={{ __html: renderMarkdown(msg.content || '') }}
                onClick={handleCodeBlockClick(onCodeAction)}
              />
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

/* 代码块点击代理，将点击委托给 code-action-btn 上的 handler */
function handleCodeBlockClick(onCodeAction: (action: string, code: string) => void): (e: React.MouseEvent) => void {
  return (e: React.MouseEvent) => {
    const target = e.target as HTMLElement;
    if (target.closest('.action-btn')) {
      onCodeAction(target.getAttribute('data-action') || '', '');
    }
  };
}

/* ---------- ChatPanel component ---------- */
interface ChatPanelProps {
  turns: PendingTurn[];
  expandDetails: boolean;
}

const ChatPanel: React.FC<ChatPanelProps> = ({ turns, expandDetails }) => {
  const { state } = useAppState();

  const handleMessageAction = (action: string, _messageId?: string) => {
    postDebug(`消息操作按钮点击: ${action}`);
  };
  const handleCodeAction = (action: string, code: string) => {
    postDebug(`代码块操作按钮点击: ${action}`);
    (window as any).postMessage?.({ type: 'debug', detail: `TODO: code action ${action}` });
  };

  return (
    <section className="panel-body" id="turnList" style={{ flex: 1, overflow: 'auto'  }}>
      {turns.length === 0 ? (
        <div className="empty-state">
          <div style={{ fontWeight: 600, marginBottom: 4 }}>会话</div>
          <div>输入内容开始聊天。</div>
        </div>
      ) : (
        turns.map(turn => (
          <React.Fragment key={turn.turnId}>
            {compileTurn(
              turn.userMessage,
              turn.assistantMessages,
              turn.events,
              expandDetails,
              handleMessageAction,
              handleCodeAction,
            )}
          </React.Fragment>
        ))
      )}
    </section>
  );
};

export default ChatPanel;
