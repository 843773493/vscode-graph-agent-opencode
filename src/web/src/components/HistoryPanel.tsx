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
  width: number;
}

interface SessionContextMenu {
  sessionId: string;
  title: string;
  x: number;
  y: number;
}

type SessionFilterMode = 'all' | 'current' | 'attachments' | 'agent' | 'named';
type SessionSortMode = 'created' | 'updated';
type SessionGroupingMode = 'workspace' | 'time';

interface SessionSection {
  id: string;
  label: string;
  sessions: Session[];
  totalCount: number;
  showMoreCount: number;
}

const CUSTOMIZATIONS_DEFAULT_HEIGHT = 286;
const CUSTOMIZATIONS_MIN_HEIGHT = 129;
const CUSTOMIZATIONS_MAX_HEIGHT = 420;
const CUSTOMIZATIONS_COLLAPSED_HEIGHT = 36;
const CUSTOMIZATIONS_RESIZING_CLASS = 'is-customizations-resizing';
const WORKSPACE_SECTION_RECENT_LIMIT = 10;
const RECENT_SECTION_DAYS = 7;

function clampCustomizationsHeight(value: number): number {
  return Math.min(CUSTOMIZATIONS_MAX_HEIGHT, Math.max(CUSTOMIZATIONS_MIN_HEIGHT, value));
}

function sessionSortTime(session: Session, sortMode: SessionSortMode): number {
  const value = sortMode === 'updated' ? session.updated_at : session.created_at;
  const time = new Date(value || '').getTime();
  return Number.isFinite(time) ? time : 0;
}

function sortSessions(sessions: Session[], sortMode: SessionSortMode): Session[] {
  return [...sessions].sort((a, b) => sessionSortTime(b, sortMode) - sessionSortTime(a, sortMode));
}

function workspaceSectionLabel(workspaceId: string, workspaceName: string): string {
  if (workspaceName) {
    return workspaceName;
  }
  return workspaceId || 'workspace';
}

function buildWorkspaceSections(
  sessions: Session[],
  workspaceName: string,
  capped: boolean,
): SessionSection[] {
  const groups = new Map<string, Session[]>();
  for (const session of sessions) {
    const workspaceId = session.workspace_id || 'workspace';
    groups.set(workspaceId, [...(groups.get(workspaceId) ?? []), session]);
  }

  return [...groups.entries()].map(([workspaceId, groupSessions]) => {
    const visibleSessions =
      capped && groupSessions.length > WORKSPACE_SECTION_RECENT_LIMIT
        ? groupSessions.slice(0, WORKSPACE_SECTION_RECENT_LIMIT)
        : groupSessions;
    return {
      id: `workspace:${workspaceId}`,
      label: workspaceSectionLabel(workspaceId, workspaceName),
      sessions: visibleSessions,
      totalCount: groupSessions.length,
      showMoreCount: groupSessions.length - visibleSessions.length,
    };
  });
}

function buildTimeSections(sessions: Session[], sortMode: SessionSortMode): SessionSection[] {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfRecent = startOfToday - RECENT_SECTION_DAYS * 86_400_000;
  const recent: Session[] = [];
  const older: Session[] = [];

  for (const session of sessions) {
    if (sessionSortTime(session, sortMode) >= startOfRecent && recent.length < WORKSPACE_SECTION_RECENT_LIMIT) {
      recent.push(session);
    } else {
      older.push(session);
    }
  }

  const sections: SessionSection[] = [];
  if (recent.length > 0) {
    sections.push({
      id: 'time:recent',
      label: '最近',
      sessions: recent,
      totalCount: recent.length,
      showMoreCount: 0,
    });
  }
  if (older.length > 0) {
    sections.push({
      id: 'time:older',
      label: '更早',
      sessions: older,
      totalCount: older.length,
      showMoreCount: 0,
    });
  }
  return sections;
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
  width,
}: HistoryPanelProps) {
  const [contextMenu, setContextMenu] = useState<SessionContextMenu | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [filterMenuOpen, setFilterMenuOpen] = useState(false);
  const [filterMode, setFilterMode] = useState<SessionFilterMode>('all');
  const [sortMode, setSortMode] = useState<SessionSortMode>('updated');
  const [groupingMode, setGroupingMode] = useState<SessionGroupingMode>('workspace');
  const [workspaceGroupCapped, setWorkspaceGroupCapped] = useState(true);
  const [collapsedSectionIds, setCollapsedSectionIds] = useState<Set<string>>(() => new Set());
  const [customizationNotice, setCustomizationNotice] = useState('');
  const [customizationsCollapsed, setCustomizationsCollapsed] = useState(false);
  const [customizationsHeight, setCustomizationsHeight] = useState(CUSTOMIZATIONS_DEFAULT_HEIGHT);
  const filterControlRef = useRef<HTMLElement | null>(null);
  const cleanupCustomizationsResizeRef = useRef<(() => void) | null>(null);
  const filteredSessions = useMemo(() => {
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
  const sortedFilteredSessions = useMemo(
    () => sortSessions(filteredSessions, sortMode),
    [filteredSessions, sortMode],
  );
  const sessionSections = useMemo(
    () =>
      groupingMode === 'workspace'
        ? buildWorkspaceSections(sortedFilteredSessions, workspaceName, workspaceGroupCapped)
        : buildTimeSections(sortedFilteredSessions, sortMode),
    [groupingMode, sortMode, sortedFilteredSessions, workspaceGroupCapped, workspaceName],
  );
  const visibleSessionCount = sessionSections.reduce(
    (total, section) => total + section.sessions.length,
    0,
  );
  const matchingSessionCount = filteredSessions.length;

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
    return () => {
      cleanupCustomizationsResizeRef.current?.();
    };
  }, []);

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
  const applySortMode = (mode: SessionSortMode, label: string) => {
    setSortMode(mode);
    setFilterMenuOpen(false);
    onStatusChange(`已排序会话: ${label}`);
  };
  const applyGroupingMode = (mode: SessionGroupingMode, label: string) => {
    setGroupingMode(mode);
    setFilterMenuOpen(false);
    onStatusChange(`已分组会话: ${label}`);
  };
  const toggleWorkspaceGroupCapping = (capped: boolean) => {
    setWorkspaceGroupCapped(capped);
    setFilterMenuOpen(false);
    onStatusChange(capped ? '仅显示最近工作区会话' : '显示全部工作区会话');
  };
  const toggleSessionSection = (sectionId: string) => {
    setCollapsedSectionIds((prev) => {
      const next = new Set(prev);
      if (next.has(sectionId)) {
        next.delete(sectionId);
      } else {
        next.add(sectionId);
      }
      return next;
    });
  };
  const collapseAllSessionSections = () => {
    setCollapsedSectionIds(new Set(sessionSections.map((section) => section.id)));
    setFilterMenuOpen(false);
    onStatusChange('已折叠全部会话分组');
  };
  const showCustomizationNotice = (label: string) => {
    const message = `${label} 由 VS Code Sessions 服务提供，Web 端暂未接入`;
    setCustomizationNotice(message);
    onStatusChange(message);
  };
  const startCustomizationsResize = (event: React.PointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    cleanupCustomizationsResizeRef.current?.();

    const startY = event.clientY;
    const startHeight = customizationsHeight;

    const handlePointerMove = (moveEvent: PointerEvent) => {
      const deltaY = moveEvent.clientY - startY;
      setCustomizationsHeight(clampCustomizationsHeight(startHeight - deltaY));
    };

    const finishResize = () => {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', finishResize);
      window.removeEventListener('pointercancel', finishResize);
      document.body.classList.remove(CUSTOMIZATIONS_RESIZING_CLASS);
      cleanupCustomizationsResizeRef.current = null;
    };

    document.body.classList.add(CUSTOMIZATIONS_RESIZING_CLASS);
    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', finishResize);
    window.addEventListener('pointercancel', finishResize);
    cleanupCustomizationsResizeRef.current = finishResize;
  };
  const displayedCustomizationsHeight = customizationsCollapsed
    ? CUSTOMIZATIONS_COLLAPSED_HEIGHT
    : customizationsHeight;

  return (
    <aside className="history-panel" style={{ flexBasis: width, width }}>
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
                <div className="sessions-menu-group-title">排序</div>
                <button
                  type="button"
                  className={sortMode === 'created' ? 'active' : ''}
                  role="menuitemradio"
                  aria-checked={sortMode === 'created'}
                  onClick={() => applySortMode('created', '按创建时间')}
                >
                  按创建时间
                </button>
                <button
                  type="button"
                  className={sortMode === 'updated' ? 'active' : ''}
                  role="menuitemradio"
                  aria-checked={sortMode === 'updated'}
                  onClick={() => applySortMode('updated', '按更新时间')}
                >
                  按更新时间
                </button>
                <div className="sessions-menu-separator" />
                <div className="sessions-menu-group-title">分组</div>
                <button
                  type="button"
                  className={groupingMode === 'workspace' ? 'active' : ''}
                  role="menuitemradio"
                  aria-checked={groupingMode === 'workspace'}
                  onClick={() => applyGroupingMode('workspace', '按工作区')}
                >
                  按工作区
                </button>
                <button
                  type="button"
                  className={groupingMode === 'time' ? 'active' : ''}
                  role="menuitemradio"
                  aria-checked={groupingMode === 'time'}
                  onClick={() => applyGroupingMode('time', '按时间')}
                >
                  按时间
                </button>
                {groupingMode === 'workspace' ? (
                  <>
                    <div className="sessions-menu-separator" />
                    <button
                      type="button"
                      className={workspaceGroupCapped ? 'active' : ''}
                      role="menuitemradio"
                      aria-checked={workspaceGroupCapped}
                      onClick={() => toggleWorkspaceGroupCapping(true)}
                    >
                      显示最近会话
                    </button>
                    <button
                      type="button"
                      className={!workspaceGroupCapped ? 'active' : ''}
                      role="menuitemradio"
                      aria-checked={!workspaceGroupCapped}
                      onClick={() => toggleWorkspaceGroupCapping(false)}
                    >
                      显示全部会话
                    </button>
                  </>
                ) : null}
                <div className="sessions-menu-separator" />
                <button
                  type="button"
                  role="menuitem"
                  onClick={collapseAllSessionSections}
                >
                  全部折叠
                </button>
                <div className="sessions-menu-separator" />
                <div className="sessions-menu-group-title">筛选</div>
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
            {matchingSessionCount === 0 ? (
              <div className="empty-state small">暂无会话</div>
            ) : (
              <>
                <div className="history-list-summary">
                  {visibleSessionCount}/{matchingSessionCount}
                </div>
                {sessionSections.map((section) => {
                  const collapsed = collapsedSectionIds.has(section.id);
                  return (
                    <section className="history-session-section" key={section.id}>
                      <button
                        type="button"
                        className={`history-section-title${collapsed ? ' collapsed' : ''}`}
                        aria-expanded={!collapsed}
                        onClick={() => toggleSessionSection(section.id)}
                      >
                        <span className="history-section-chevron" aria-hidden="true">
                          {collapsed ? '›' : '⌄'}
                        </span>
                        <span className="history-section-label">{section.label}</span>
                        <span className="history-section-count">
                          {section.sessions.length}/{section.totalCount}
                        </span>
                      </button>
                      {!collapsed ? (
                        <>
                          <ul className="session-list">
                            {section.sessions.map(session => {
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
                          {groupingMode === 'workspace' && section.showMoreCount > 0 ? (
                            <button
                              type="button"
                              className="session-show-more-button"
                              onClick={() => toggleWorkspaceGroupCapping(false)}
                            >
                              显示全部 {section.showMoreCount} 个更多会话
                            </button>
                          ) : null}
                          {groupingMode === 'workspace' &&
                          !workspaceGroupCapped &&
                          section.totalCount > WORKSPACE_SECTION_RECENT_LIMIT ? (
                            <button
                              type="button"
                              className="session-show-more-button"
                              onClick={() => toggleWorkspaceGroupCapping(true)}
                            >
                              仅显示最近会话
                            </button>
                          ) : null}
                        </>
                      ) : null}
                    </section>
                  );
                })}
              </>
            )}
          </section>
        </div>
        {!customizationsCollapsed ? (
          <button
            type="button"
            className="history-customizations-resize-sash"
            title="拖拽调整会话列表和自定义区域大小，双击还原"
            aria-label="调整会话列表和自定义区域大小"
            onPointerDown={startCustomizationsResize}
            onDoubleClick={() => setCustomizationsHeight(CUSTOMIZATIONS_DEFAULT_HEIGHT)}
          />
        ) : null}
        <footer
          className={`sessions-customizations${customizationsCollapsed ? ' collapsed' : ''}`}
          style={{ flexBasis: displayedCustomizationsHeight, height: displayedCustomizationsHeight }}
        >
          <button
            type="button"
            className={`customizations-header${customizationsCollapsed ? ' collapsed' : ''}`}
            aria-expanded={!customizationsCollapsed}
            onClick={() => setCustomizationsCollapsed((collapsed) => !collapsed)}
          >
            <span className="customizations-title">自定义</span>
            <span className="customizations-chevron" aria-hidden="true">
              {customizationsCollapsed ? '›' : '⌄'}
            </span>
          </button>
          {!customizationsCollapsed ? (
            <div className="customizations-body">
              <button type="button" className="customization-link" onClick={() => showCustomizationNotice('概述')}>⌂ <span>概述</span></button>
              <button type="button" className="customization-link" onClick={() => showCustomizationNotice('智能体')}>◇ <span>智能体</span><span className="customization-count">{String(Math.max(0, sessions.length ? 1 : 0))}</span></button>
              <button type="button" className="customization-link" onClick={() => showCustomizationNotice('技能')}>♢ <span>技能</span><span className="customization-count">24</span></button>
              <button type="button" className="customization-link" onClick={() => showCustomizationNotice('指令')}>☰ <span>指令</span><span className="customization-count">1</span></button>
              <button type="button" className="customization-link" onClick={() => showCustomizationNotice('挂钩')}>⚡ <span>挂钩</span></button>
              <button type="button" className="customization-link" onClick={() => showCustomizationNotice('MCP 服务器')}>▤ <span>MCP 服务器</span><span className="customization-count">1</span></button>
              <button type="button" className="customization-link" onClick={() => showCustomizationNotice('插件')}>⌘ <span>插件</span></button>
              <button type="button" className="customization-link" onClick={() => showCustomizationNotice('工具')}>⚒ <span>工具</span></button>
              {customizationNotice ? (
                <div className="customization-notice" role="status">
                  {customizationNotice}
                </div>
              ) : null}
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
