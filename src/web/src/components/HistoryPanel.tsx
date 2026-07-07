import React, { useEffect, useState } from 'react';
import type { Session } from '../types/backend';
import type { SessionAttachmentSummary } from '../types/frontend';
import { formatDateTime } from '../utils/format';

interface HistoryPanelProps {
  sessions: Session[];
  currentSessionId: string;
  onSelectSession: (sessionId: string) => void;
  onRenameSession: (sessionId: string, currentTitle: string) => void;
  onDeleteSession: (sessionId: string, currentTitle: string) => void;
  onStatusChange: (message: string) => void;
  isOpen: boolean;
  onClose: () => void;
  workspaceName: string;
  workspaceRoot: string;
  activeSession: Session | null;
  sessionAttachmentSummaries: Map<string, SessionAttachmentSummary>;
}

interface SessionContextMenu {
  sessionId: string;
  title: string;
  x: number;
  y: number;
}

function sessionTone(_session: Session, isActive: boolean): 'active' | 'warning' | 'danger' | 'neutral' {
  if (isActive) {
    return 'active';
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

function AttachmentSummaryBadge({
  summary,
}: {
  summary: SessionAttachmentSummary | undefined;
}): React.ReactNode {
  if (!summary || summary.count === 0) {
    return null;
  }
  const label = summary.names.length > 0 ? summary.names.join(', ') : '附件';
  return (
    <span className="badge neutral session-attachment-badge" title={label}>
      附件 {summary.count}
    </span>
  );
}

function fallbackCopyText(text: string): void {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', 'true');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  textarea.style.top = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  // TODO: 兼容非安全上下文下 Clipboard API 不可用的浏览器，后续全站 HTTPS 后移除。
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) {
    throw new Error('浏览器拒绝复制会话 ID');
  }
}

async function copyTextToClipboard(text: string): Promise<void> {
  let clipboardError: unknown = null;
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (error) {
      clipboardError = error;
    }
  }

  try {
    fallbackCopyText(text);
  } catch (fallbackError) {
    if (clipboardError) {
      const clipboardMessage = clipboardError instanceof Error ? clipboardError.message : String(clipboardError);
      const fallbackMessage = fallbackError instanceof Error ? fallbackError.message : String(fallbackError);
      throw new Error(`Clipboard API 失败：${clipboardMessage}；兼容复制失败：${fallbackMessage}`);
    }
    throw fallbackError;
  }
}

function SessionButton({
  session,
  isActive,
  summary,
  onSelectSession,
  onOpenMenu,
  focus,
}: {
  session: Session;
  isActive: boolean;
  summary: SessionAttachmentSummary | undefined;
  onSelectSession: (sessionId: string) => void;
  onOpenMenu: (session: Session, x: number, y: number) => void;
  focus?: boolean;
}): React.ReactNode {
  const attachmentNames = summary?.names.join(', ') ?? '';
  return (
    <button
      type="button"
      className={`session-item${isActive ? ' active' : ''}${focus ? ' session-item-focus' : ''}`}
      onClick={() => onSelectSession(session.session_id)}
      onContextMenu={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onOpenMenu(session, event.clientX, event.clientY);
      }}
    >
      <div className="session-title-row">
        <span className="session-title">{session.title || '未命名'}</span>
        <span className="session-badges">
          <AttachmentSummaryBadge summary={summary} />
          {sessionLabel(session, isActive)}
        </span>
      </div>
      <div className="session-meta">
        <span className="session-time">
          {formatDateTime(session.updated_at || session.created_at) || 'now'}
        </span>
        <span className="session-id">{session.session_id}</span>
      </div>
      {attachmentNames ? (
        <div className="session-attachment-names" title={attachmentNames}>
          {attachmentNames}
        </div>
      ) : null}
    </button>
  );
}

export default function HistoryPanel({
  sessions,
  currentSessionId,
  onSelectSession,
  onRenameSession,
  onDeleteSession,
  onStatusChange,
  isOpen,
  onClose,
  workspaceName,
  workspaceRoot,
  activeSession,
  sessionAttachmentSummaries,
}: HistoryPanelProps) {
  const [contextMenu, setContextMenu] = useState<SessionContextMenu | null>(null);

  useEffect(() => {
    if (!contextMenu) {
      return;
    }

    const closeMenu = () => setContextMenu(null);
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        closeMenu();
      }
    };

    window.addEventListener('pointerdown', closeMenu);
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('pointerdown', closeMenu);
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [contextMenu]);

  useEffect(() => {
    if (!isOpen) {
      setContextMenu(null);
    }
  }, [isOpen]);

  if (!isOpen) {
    return null;
  }
  const listedSessions = sessions.filter(
    (session) => session.session_id !== currentSessionId,
  );
  const openSessionMenu = (session: Session, x: number, y: number) => {
    const menuWidth = 156;
    const menuHeight = 108;
    setContextMenu({
      sessionId: session.session_id,
      title: session.title || '',
      x: Math.max(8, Math.min(x, window.innerWidth - menuWidth - 8)),
      y: Math.max(8, Math.min(y, window.innerHeight - menuHeight - 8)),
    });
  };
  const handleRenameFromMenu = () => {
    if (!contextMenu) {
      return;
    }
    const target = contextMenu;
    setContextMenu(null);
    onRenameSession(target.sessionId, target.title);
  };
  const handleDeleteFromMenu = () => {
    if (!contextMenu) {
      return;
    }
    const target = contextMenu;
    setContextMenu(null);
    onDeleteSession(target.sessionId, target.title);
  };
  const handleCopySessionIdFromMenu = () => {
    if (!contextMenu) {
      return;
    }
    const target = contextMenu;
    setContextMenu(null);
    void copyTextToClipboard(target.sessionId)
      .then(() => {
        onStatusChange(`已复制会话 ID: ${target.sessionId}`);
      })
      .catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        onStatusChange(`复制会话 ID 失败: ${message}`);
      });
  };

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
              <SessionButton
                session={activeSession}
                isActive
                summary={sessionAttachmentSummaries.get(activeSession.session_id)}
                onSelectSession={onSelectSession}
                onOpenMenu={openSessionMenu}
                focus
              />
            ) : (
              <div className="empty-state small">尚未选中会话</div>
            )}
          </section>

          <section className="history-section history-list-section">
            <div className="history-section-title">列表</div>
            {listedSessions.length === 0 ? (
              <div className="empty-state small">暂无其他会话</div>
            ) : (
              <ul className="session-list">
                {listedSessions.map(session => {
                  const isActive = session.session_id === currentSessionId;
                  return (
                    <li key={session.session_id}>
                      <SessionButton
                        session={session}
                        isActive={isActive}
                        summary={sessionAttachmentSummaries.get(session.session_id)}
                        onSelectSession={onSelectSession}
                        onOpenMenu={openSessionMenu}
                      />
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        </div>
        {contextMenu ? (
          <div
            className="history-session-menu"
            style={{ left: contextMenu.x, top: contextMenu.y }}
            role="menu"
            onPointerDown={(event) => event.stopPropagation()}
          >
            <button type="button" role="menuitem" onClick={handleCopySessionIdFromMenu}>
              复制会话 ID
            </button>
            <button type="button" role="menuitem" onClick={handleRenameFromMenu}>
              重命名
            </button>
            <button type="button" role="menuitem" className="danger" onClick={handleDeleteFromMenu}>
              删除
            </button>
          </div>
        ) : null}
      </div>
    </aside>
  );
}
