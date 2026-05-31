import React from 'react';
import { useAppState } from '../hooks';
import type { Message, PendingTurn, TraceEvent } from '../types';
import { escapeHtml, formatTime } from '../utils/format';
import { renderMarkdown } from '../utils/markdown';

type TraceTone = 'running' | 'done' | 'danger';

function isErrorTrace(event: TraceEvent): boolean {
  const type = String(event.event_type ?? '').toLowerCase();
  return type === 'error' || type === 'job_failed' || type === 'job_cancelled';
}

function traceTone(eventType: string): TraceTone {
  const type = String(eventType ?? '').toLowerCase();
  if (type === 'error' || type === 'job_failed' || type === 'job_cancelled') return 'danger';
  if (type === 'agent_end' || type === 'tool_call_end') return 'done';
  return 'running';
}

function traceTitle(eventType: string, payload: Record<string, unknown>): string {
  const type = String(eventType ?? '').toLowerCase();
  if (type === 'agent_start') return '开始处理';
  if (type === 'agent_step') return payload.phase ? String(payload.phase) : '执行中';
  if (type === 'tool_call_start') return `调用工具 ${String(payload.tool_name ?? 'unknown_tool')}`;
  if (type === 'tool_call_end') return `工具完成 ${String(payload.tool_name ?? 'unknown_tool')}`;
  if (type === 'file_write') return `写入文件 ${String(payload.path ?? payload.file_path ?? 'unknown path')}`;
  if (type === 'llm_request' || type === 'model_call') return `模型调用 ${String(payload.model ?? 'unknown model')}`;
  if (type === 'agent_end') return '任务结束';
  if (type === 'error') return '发生错误';
  return `事件 ${eventType}`;
}

function traceSummary(eventType: string, payload: Record<string, unknown>): string[] {
  const fields = [
    ['工具', payload.tool_name],
    ['阶段', payload.phase],
    ['模型', payload.model],
    ['文件', payload.path ?? payload.file_path],
    ['消息', payload.message],
    ['错误', payload.error],
  ] as const;
  return fields
    .filter(([, value]) => value !== undefined && value !== null && String(value).trim().length > 0)
    .map(([label, value]) => `${label}: ${String(value)}`)
    .slice(0, 3)
    .concat(eventType === 'agent_end' && payload.final_message_count !== undefined ? [`消息数: ${String(payload.final_message_count)}`] : []);
}

function TraceEventCard({ event, index }: { event: TraceEvent; index: number }): React.ReactNode {
  const payload = event.data ?? {};
  const tone = traceTone(event.event_type);
  const details = traceSummary(event.event_type, payload);

  return (
    <article className={`trace-event-card tone-${tone}`}>
      <div className="trace-event-head">
        <div className="trace-event-title-row">
          <span className={`trace-dot trace-${tone}`} />
          <span className="trace-event-title">{escapeHtml(traceTitle(event.event_type, payload))}</span>
        </div>
        <span className="badge neutral trace-event-time">{escapeHtml(displayTime(event.timestamp) || `#${index + 1}`)}</span>
      </div>
      {details.length > 0 && (
        <div className="trace-event-meta">
          {details.map(item => (
            <span key={item} className="trace-meta-pill">{escapeHtml(item)}</span>
          ))}
        </div>
      )}
      <details className="trace-event-details">
        <summary>查看原始数据</summary>
        <pre>{escapeHtml(JSON.stringify(payload, null, 2))}</pre>
      </details>
    </article>
  );
}

function TracePanel({ events }: { events: TraceEvent[] }): React.ReactNode {
  if (!events.length) return null;

  const stdout = events.filter(event => !isErrorTrace(event));
  const stderr = events.filter(event => isErrorTrace(event));

  return (
    <details className="request-container output-container" open>
      <summary className="title">
        <div className="request-main">
          <span className="request-chevron">▼</span>
          <span className="request-title">执行轨迹</span>
        </div>
        <div className="request-stats">
          <span className="badge neutral">{String(events.length)} events</span>
          <span className="badge neutral">{String(stdout.length)} stdout</span>
          <span className="badge danger">{String(stderr.length)} stderr</span>
        </div>
      </summary>
      <div className="request-details">
        {stdout.length > 0 && (
          <section className="output-stream">
            <h3 className="output-stream-title">stdout</h3>
            <div className="output-stream-body">
              {stdout.map((event, index) => (
                <TraceEventCard key={`${event.event_type}-${event.timestamp ?? index}`} event={event} index={index} />
              ))}
            </div>
          </section>
        )}
        {stderr.length > 0 && (
          <section className="output-stream">
            <h3 className="output-stream-title">stderr</h3>
            <div className="output-stream-body">
              {stderr.map((event, index) => (
                <TraceEventCard key={`${event.event_type}-${event.timestamp ?? index}`} event={event} index={index} />
              ))}
            </div>
          </section>
        )}
      </div>
    </details>
  );
}

async function copyText(text: string): Promise<void> {
  if (!text) return;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  throw new Error('当前环境不支持剪贴板写入');
}

function displayTime(value: unknown): string {
  return formatTime(value) || 'now';
}

function MessageHeader({ label, time, extra }: { label: string; time: string; extra?: React.ReactNode }): React.ReactNode {
  return (
    <div className="chat-message-head">
      <div className="chat-message-head-main">
        <span className="chat-message-role">{label}</span>
        {extra}
      </div>
      <span className="chat-message-time">{escapeHtml(time)}</span>
    </div>
  );
}

function AssistantMessageCard({ message }: { message: Message }): React.ReactNode {
  return (
    <article className="chat-message assistant">
      <MessageHeader
        label="Assistant"
        time={displayTime(message.created_at)}
        extra={<span className="badge neutral">回复</span>}
      />
      <div className="chat-message-body reply" dangerouslySetInnerHTML={{ __html: renderMarkdown(message.content || '') }} />
    </article>
  );
}

function UserMessageCard({ message }: { message: Message }): React.ReactNode {
  return (
    <article className="chat-message user">
      <MessageHeader label="You" time={displayTime(message.created_at)} extra={<span className="badge neutral">输入</span>} />
      <div className="chat-message-body user-copy">{message.content}</div>
    </article>
  );
}

function TurnCard({ turn, onPrompt, showTrace }: { turn: PendingTurn; onPrompt: (prompt: string) => void; showTrace: boolean }): React.ReactNode {
  const pending = turn.pending || turn.status === 'running';
  const assistantCount = turn.assistantMessages.length;
  const eventCount = turn.events.length;
  const statusTone = turn.status === 'error' ? 'danger' : pending ? 'warning' : 'active';
  const statusLabel = turn.status === 'error' ? '失败' : pending ? '运行中' : '已完成';

  return (
    <article className={`request-container turn-card turn-${turn.status}${pending ? ' turn-pending' : ''}`}>
      <div className="turn-card-top">
        <div className="turn-card-top-left">
          <span className={`badge ${statusTone}`}>{statusLabel}</span>
          <span className="turn-card-title">{turn.userMessage?.content?.trim() ? '用户请求' : '未命名请求'}</span>
        </div>
        <div className="turn-card-stats">
          <span className="badge neutral">{String(assistantCount)} replies</span>
          <span className="badge neutral">{String(eventCount)} events</span>
        </div>
      </div>

      {turn.userMessage && <UserMessageCard message={turn.userMessage} />}

      <section className="turn-section-body">
        {assistantCount > 0 ? (
          turn.assistantMessages.map(message => <AssistantMessageCard key={message.message_id} message={message} />)
        ) : (
          <div className="chat-message assistant pending-message">
            <MessageHeader label="Assistant" time={displayTime(new Date().toISOString())} extra={<span className="badge warning">思考中</span>} />
            <div className="chat-message-body reply">正在整理上下文并生成结果…</div>
          </div>
        )}
      </section>

      {showTrace && turn.events.length > 0 && <TracePanel events={turn.events} />}
    </article>
  );
}

interface ChatPanelProps {
  turns: PendingTurn[];
  expandDetails: boolean;
}

export default function ChatPanel({ turns, expandDetails }: ChatPanelProps) {
  const { sendMessage } = useAppState();

  const handlePrompt = React.useCallback((prompt: string) => {
    sendMessage(prompt);
  }, [sendMessage]);

  return (
    <section className="chat-stream" data-expand-details={String(expandDetails)}>
      {turns.length === 0 ? (
        <div className="empty-state">
          <div className="empty-state-title">对话区</div>
          <div>输入消息后，这里会显示完整的会话卡片、回复和 trace 细节。</div>
        </div>
      ) : (
        turns.map(turn => <TurnCard key={turn.turnId} turn={turn} onPrompt={handlePrompt} showTrace={expandDetails} />)
      )}
      <div className="chat-stream-bottom-spacer" />
    </section>
  );
}