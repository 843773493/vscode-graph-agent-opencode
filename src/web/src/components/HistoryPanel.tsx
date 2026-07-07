import React, { useEffect, useMemo, useRef, useState } from 'react';
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
  onCreateSession: () => void;
}

interface SessionContextMenu {
  sessionId: string;
  title: string;
  x: number;
  y: number;
}

type SessionFilterMode = 'all' | 'current' | 'attachments' | 'agent' | 'named';

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
        <span className="session-status-dot" aria-hidden="true" />
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
  onCreateSession,
}: HistoryPanelProps) {
  const [contextMenu, setContextMenu] = useState<SessionContextMenu | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [filterMenuOpen, setFilterMenuOpen] = useState(false);
  const [filterMode, setFilterMode] = useState<SessionFilterMode>('all');
  const [customizationNotice, setCustomizationNotice] = useState('');
  const filterControlRef = useRef<HTMLElement | null>(null);
  const listedSessions = useMemo(() => {
    const normalizedQuery = searchQuery.trim().toLocaleLowerCase();
    const matchingSessions = sessions.filter((session) => {
      if (filterMode === 'current' && session.session_id !== currentSessionId) {
        return false;
      }
      if (
        filterMode === 'attachments' &&
        !sessionAttachmentSummaries.get(session.session_id)?.count
      ) {
        return false;
      }
      if (
        filterMode === 'agent' &&
        activeSession?.current_agent_id &&
        session.current_agent_id !== activeSession.current_agent_id
      ) {
        return false;
      }
      if (filterMode === 'named' && session.title_source === 'default') {
        return false;
      }
      if (!normalizedQuery) {
        return true;
      }
      return `${session.title} ${session.session_id}`
        .toLocaleLowerCase()
        .includes(normalizedQuery);
    });
    if (
      currentSessionId &&
      !matchingSessions.some((session) => session.session_id === currentSessionId)
    ) {
      const currentSession = sessions.find(
        (session) => session.session_id === currentSessionId,
      );
      return currentSession ? [currentSession, ...matchingSessions] : matchingSessions;
    }
    return matchingSessions;
  }, [
    activeSession?.current_agent_id,
    currentSessionId,
    filterMode,
    searchQuery,
    sessionAttachmentSummaries,
    sessions,
  ]);

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
      setFilterMenuOpen(false);
    }
  }, [isOpen]);

  useEffect(() => {
    if (!filterMenuOpen) {
      return;
    }

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (
        target instanceof Node &&
        filterControlRef.current?.contains(target)
      ) {
        return;
      }
      setFilterMenuOpen(false);
    };

    window.addEventListener('pointerdown', handlePointerDown);
    return () => window.removeEventListener('pointerdown', handlePointerDown);
  }, [filterMenuOpen]);

  if (!isOpen) {
    return null;
  }
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
  const applyFilterMode = (mode: SessionFilterMode, label: string) => {
    setFilterMode(mode);
    setFilterMenuOpen(false);
    onStatusChange(`已筛选会话: ${label}`);
  };
  const showCustomizationNotice = (label: string) => {
    const message = `${label} 由 VS Code Sessions 服务提供，Web 端暂未接入`;
    setCustomizationNotice(message);
    onStatusChange(message);
  };

  return (
    <aside className="history-panel">
      <div className="history-panel-shell">
        <header className="panel-header history-panel-header">
          <span className="panel-title">会话</span>
          <section ref={filterControlRef} className="sessions-sidebar-actions" aria-label="会话操作">
            <button
              type="button"
              className="new-session-pill"
              onClick={onCreateSession}
              title="新建会话"
            >
              <span>新</span>
              <kbd>Ctrl+N</kbd>
            </button>
            <button
              type="button"
              className={`sidebar-icon-button${filterMenuOpen ? ' active' : ''}`}
              title="筛选会话"
              aria-label="筛选会话"
              aria-haspopup="menu"
              aria-expanded={filterMenuOpen}
              onClick={() => setFilterMenuOpen((open) => !open)}
            >
              ⌁
            </button>
            <button
              type="button"
              className={`sidebar-icon-button${searchOpen ? ' active' : ''}`}
              title="搜索会话"
              aria-label="搜索会话"
              aria-pressed={searchOpen}
              onClick={() => {
                setSearchOpen((open) => {
                  const nextOpen = !open;
                  if (!nextOpen) {
                    setSearchQuery('');
                  }
                  return nextOpen;
                });
              }}
            >
              ⌕
            </button>
            <button type="button" className="sidebar-icon-button sidebar-close-button" title="关闭侧栏" onClick={onClose} aria-label="关闭侧栏">
              ◐
            </button>
            {filterMenuOpen ? (
              <div className="sessions-filter-menu" role="menu">
                <button
                  type="button"
                  className={filterMode === 'all' ? 'active' : ''}
                  role="menuitemradio"
                  aria-checked={filterMode === 'all'}
                  onClick={() => applyFilterMode('all', '全部会话')}
                >
                  全部会话
                </button>
                <button
                  type="button"
                  className={filterMode === 'current' ? 'active' : ''}
                  role="menuitemradio"
                  aria-checked={filterMode === 'current'}
                  onClick={() => applyFilterMode('current', '当前会话')}
                >
                  当前会话
                </button>
                <button
                  type="button"
                  className={filterMode === 'attachments' ? 'active' : ''}
                  role="menuitemradio"
                  aria-checked={filterMode === 'attachments'}
                  onClick={() => applyFilterMode('attachments', '包含附件')}
                >
                  包含附件
                </button>
                <button
                  type="button"
                  className={filterMode === 'agent' ? 'active' : ''}
                  role="menuitemradio"
                  aria-checked={filterMode === 'agent'}
                  onClick={() => applyFilterMode('agent', '当前 Agent')}
                >
                  当前 Agent
                </button>
                <button
                  type="button"
                  className={filterMode === 'named' ? 'active' : ''}
                  role="menuitemradio"
                  aria-checked={filterMode === 'named'}
                  onClick={() => applyFilterMode('named', '已命名会话')}
                >
                  已命名会话
                </button>
              </div>
            ) : null}
          </section>
        </header>

        <div className="panel-body history-panel-body">
          {searchOpen ? (
            <label className="sessions-search-box">
              <span>查找会话</span>
              <input
                autoFocus
                type="search"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
                placeholder="按标题或 ID 搜索"
              />
            </label>
          ) : null}
          <section className="workspace-summary-card">
            <div className="workspace-summary-name">{workspaceName || 'workspace'}</div>
            <div className="workspace-summary-path" title={workspaceRoot || undefined}>
              {workspaceRoot || '路径未加载'}
            </div>
          </section>

          <section className="history-section history-list-section">
            <div className="history-section-title">
              <span>{workspaceName || 'workspace'}</span>
              <span className="history-section-count">
                {listedSessions.length}/{sessions.length}
              </span>
            </div>
            {listedSessions.length === 0 ? (
              <div className="empty-state small">暂无会话</div>
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
        <footer className="sessions-customizations">
          <div className="customizations-title">自定义</div>
          <button type="button" className="customization-link" onClick={() => showCustomizationNotice('概述')}>⌂ <span>概述</span></button>
          <button type="button" className="customization-link" onClick={() => showCustomizationNotice('智能体')}>◇ <span>智能体</span><span className="customization-count">{String(Math.max(0, sessions.length ? 1 : 0))}</span></button>
          <button type="button" className="customization-link" onClick={() => showCustomizationNotice('技能')}>♢ <span>技能</span><span className="customization-count">24</span></button>
          <button type="button" className="customization-link" onClick={() => showCustomizationNotice('指令')}>☰ <span>指令</span><span className="customization-count">1</span></button>
          <button type="button" className="customization-link" onClick={() => showCustomizationNotice('挂钩')}>⚡ <span>挂钩</span></button>
          <button type="button" className="customization-link" onClick={() => showCustomizationNotice('MCP 服务器')}>▤ <span>MCP 服务器</span><span className="customization-count">1</span></button>
          <button type="button" className="customization-link" onClick={() => showCustomizationNotice('插件')}>⌘ <span>插件</span></button>
          {customizationNotice ? (
            <div className="customization-notice" role="status">
              {customizationNotice}
            </div>
          ) : null}
        </footer>
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
