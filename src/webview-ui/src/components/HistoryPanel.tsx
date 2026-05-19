import React from 'react';
import type { Session } from '../types';
import { formatTime } from '../utils/format';

interface HistoryPanelProps {
  sessions: Session[];
  currentSessionId: string;
  onSelectSession: (sessionId: string) => void;
  isOpen: boolean;
}

function sessionStatusBadge(session: Session, isActive: boolean): React.ReactNode {
  if (isActive) return <span className="badge active">Active</span>;
  const status = String(session.status ?? '').toLowerCase();
  if (status.includes('fail') || status.includes('error')) return <span className="badge danger">Failed</span>;
  if (status.includes('progress') || status.includes('run')) return <span className="badge warning">Running</span>;
  return <span className="badge neutral">Ready</span>;
}

export default function HistoryPanel({ sessions, currentSessionId, onSelectSession, isOpen }: HistoryPanelProps) {
  return (
    <aside className={`history-panel${isOpen ? ' open' : ''}`}>
      <div className="panel-header">历史会话</div>
      <div className="panel-body">
        {sessions.length === 0 ? (
          <div className="empty-state small">暂无历史会话</div>
        ) : (
          <ul className="session-list">
            {sessions.map(session => {
              const isActive = session.session_id === currentSessionId;
              return (
                <li key={session.session_id}>
                  <button type="button" className={`session-item${isActive ? ' active' : ''}`} onClick={() => onSelectSession(session.session_id)}>
                    <div className="session-title-row">
                      <span className="session-title">{session.title || '未命名会话'}</span>
                      {sessionStatusBadge(session, isActive)}
                    </div>
                    <div className="session-meta">
                      <span className="session-time">{formatTime(session.updated_at || session.created_at)}</span>
                      <span className="session-id">{session.session_id}</span>
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
}
