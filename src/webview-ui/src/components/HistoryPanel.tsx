import type { Session } from '../types';
import SessionTree from './SessionTree';

interface HistoryPanelProps {
  sessions: Session[];
  currentSessionId: string;
  onSelectSession: (sessionId: string) => void;
  isOpen: boolean;
  onClose: () => void;
  workspaceName: string;
  workspaceRoot: string;
  activeSession: Session | null;
  onSetSessionParent: (sessionId: string, parentSessionId: string | null) => Promise<void>;
}
export default function HistoryPanel({ sessions, currentSessionId, onSelectSession, onSetSessionParent, isOpen, onClose, workspaceName, workspaceRoot, activeSession }: HistoryPanelProps) {
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

          <section className="history-section history-list-section">
            <div className="history-section-title">会话</div>
            {sessions.length === 0 ? (
              <div className="empty-state small">暂无会话</div>
            ) : (
              <SessionTree
                sessions={sessions}
                currentSessionId={currentSessionId}
                onSelectSession={onSelectSession}
                onSetSessionParent={onSetSessionParent}
              />
            )}
          </section>
        </div>
      </div>
    </aside>
  );
}
