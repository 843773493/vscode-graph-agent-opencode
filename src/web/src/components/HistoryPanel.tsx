import React from 'react';
import type { Session } from '../types/backend';
import { formatTime } from '../utils/format';

interface HistoryPanelProps {
  sessions: Session[];
  currentSessionId: string;
  onSelectSession: (sessionId: string) => void;
  isOpen: boolean;
  onClose: () => void;
  workspaceName: string;
  workspaceRoot: string;
  activeSession: Session | null;
}

function sessionTone(session: Session, isActive: boolean): 'active' | 'warning' | 'danger' | 'neutral' {
  if (isActive) {
    return 'active';
  }

  const status = String(session.status ?? '').toLowerCase();
  if (status.includes('fail') || status.includes('error')) {
    return 'danger';
  }

  if (status.includes('progress') || status.includes('run')) {
    return 'warning';
  }

  return 'neutral';
}

function sessionLabel(session: Session, isActive: boolean): React.ReactNode {
  const tone = sessionTone(session, isActive);
  if (tone === 'active') return <span className="badge active">当前</span>;
  if (tone === 'danger') return <span className="badge danger">失败</span>;
  if (tone === 'warning') return <span className="badge warning">运行中</span>;
  return <span className="badge neutral">就绪</span>;
}

export default function HistoryPanel({ sessions, currentSessionId, onSelectSession, isOpen, onClose, workspaceName, workspaceRoot, activeSession }: HistoryPanelProps) {
  if (!isOpen) {
    return null;
  }

  return (
    <aside className="history-panel">
      <div className="history-panel-shell">
        <header className="panel-header history-panel-header">
          <div className="panel-header-main">
            <span className="panel-title">历史</span>
            <span className="badge neutral panel-count-badge">{String(sessions.length)}</span>
          </div>
          <button type="button" className="panel-icon-button" title="关闭历史栏" onClick={onClose} aria-label="关闭历史栏">
            ×
          </button>
        </header>

        <div className="panel-body history-panel-body">
          <section className="workspace-summary-card">
            <div className="workspace-summary-title">工作区</div>
            <div className="workspace-summary-name">{workspaceName || 'workspace'}</div>
            <div className="workspace-summary-path">{workspaceRoot || '路径未加载'}</div>
            <div className="workspace-summary-stats">
              <span className="badge neutral">{String(sessions.length)} 会话</span>
              <span className="badge neutral">{activeSession ? '已选中' : '未选中'}</span>
            </div>
          </section>

          <section className="history-section">
            <div className="history-section-title">当前</div>
            {activeSession ? (
              <button type="button" className="session-item session-item-focus" onClick={() => onSelectSession(activeSession.session_id)}>
                <div className="session-title-row">
                  <span className="session-title">{activeSession.title || '未命名'}</span>
                  {sessionLabel(activeSession, true)}
                </div>
                <div className="session-meta">
                  <span className="session-time">{formatTime(activeSession.updated_at || activeSession.created_at) || 'now'}</span>
                  <span className="session-id">{activeSession.session_id}</span>
                </div>
              </button>
            ) : (
              <div className="empty-state small">尚未选中会话</div>
            )}
          </section>

          <section className="history-section history-list-section">
            <div className="history-section-title">列表</div>
            {sessions.length === 0 ? (
              <div className="empty-state small">暂无会话</div>
            ) : (
              <ul className="session-list">
                {sessions.map(session => {
                  const isActive = session.session_id === currentSessionId;
                  return (
                    <li key={session.session_id}>
                      <button type="button" className={`session-item${isActive ? ' active' : ''}`} onClick={() => onSelectSession(session.session_id)}>
                        <div className="session-title-row">
                          <span className="session-title">{session.title || '未命名'}</span>
                          {sessionLabel(session, isActive)}
                        </div>
                        <div className="session-meta">
                          <span className="session-time">{formatTime(session.updated_at || session.created_at) || 'now'}</span>
                          <span className="session-id">{session.session_id}</span>
                        </div>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        </div>
      </div>
    </aside>
  );
}
