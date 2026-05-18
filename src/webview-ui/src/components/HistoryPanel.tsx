import React, { useRef, useEffect } from 'react';
import type { Session, TraceEvent, PendingTurn } from '../types';
import { formatTime, escapeHtml } from '../utils/format';
import { postDebug } from '../vscode';

interface HistoryPanelProps {
  sessions: Session[];
  currentSessionId: string;
  onSelectSession: (sessionId: string) => void;
  isOpen: boolean;
}

function isActiveSession(session: Session, sessionId: string): string {
  return session.session_id === sessionId ? 'active' : '';
}

function sessionStatusBadge(session: Session, isActive: boolean): string {
  if (isActive) return '<span class="badge active">Active</span>';
  const status = String(session?.status ?? '').toLowerCase();
  if (status.includes('fail') || status.includes('error')) return '<span class="badge danger">Failed</span>';
  if (status.includes('progress') || status.includes('run')) return '<span class="badge warning">Running</span>';
  return '<span class="badge neutral">Ready</span>';
}

const HistoryPanel: React.FC<HistoryPanelProps> = ({ sessions, currentSessionId, onSelectSession, isOpen }) => {
  const listRef = useRef<HTMLDivElement>(null);
  const [agentName, setAgentName] = React.useState('');

  useEffect(() => {
    if (isOpen) {
      postDebug('历史会话面板打开');
    }
  }, [isOpen]);

  const sorted = React.useMemo(
    () => [...sessions].sort(
      (a, b) => new Date(b.updated_at || b.created_at || '').getTime() - new Date(a.updated_at || a.created_at || '').getTime()
    ),
    [sessions]
  );

  return (
    <aside className={`history-panel${isOpen ? ' open' : ''}`} id="historyPanel">
      <div className="panel-header">历史会话</div>
      <div className="panel-body" id="sessionList" ref={listRef}>
        {sorted.length === 0 ? (
          <div className="empty-state small">暂无历史会话</div>
        ) : (
          <ul className="session-list" style={{ listStyle: 'none', padding: 0, margin: 0 }}>
            {sorted.map(session => {
              const isActive = session.session_id === currentSessionId;
              const title = session.title || '未命名会话';
              const time = formatTime(session.updated_at || session.created_at);
              return (
                <li key={session.session_id} style={{ listStyle: 'none', margin: 0 }}>
                  <button
                    className={`session-item${isActive ? ' active' : ''}`}
                    data-select-session={escapeHtml(session.session_id)}
                    onClick={() => onSelectSession(session.session_id)}
                    style={{ width: '100%', textAlign: 'left', border: 'none', background: 'transparent', padding: 0 }}
                  >
                    <div style={{ fontWeight: 600, fontSize: 12, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {escapeHtml(title)}
                    </div>
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, color: 'var(--muted)', fontSize: 11 }}>
                      {sessionStatusBadge(session, isActive)}
                      <span className="session-time" style={{ whiteSpace: 'nowrap' }}>{escapeHtml(time)}</span>
                      <span
                        className="session-id"
                        style={{ fontSize: 11, color: 'var(--vscode-descriptionForeground)', opacity: 0.7, marginLeft: 8, fontFamily: 'var(--vscode-editor-font-family)', userSelect: 'text', cursor: 'text' }}
                        onMouseDown={(e) => e.stopPropagation()}
                      >
                        {escapeHtml(session.session_id)}
                      </span>
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </aside>
  );
};

export default HistoryPanel;
