import React, { useEffect, useMemo, useRef, useState } from 'react';
import type { GatewayWorkspace, Session } from '../types/backend';
import type { SessionAttachmentSummary } from '../types/frontend';
import AgentSessionsCustomizations, {
  CUSTOMIZATIONS_COLLAPSED_HEIGHT,
  CUSTOMIZATIONS_DEFAULT_HEIGHT,
  CUSTOMIZATIONS_RESIZING_CLASS,
  clampCustomizationsHeight,
} from './agentSessions/AgentSessionsCustomizations';
import AgentSessionsContextMenus, {
  type SessionContextMenu,
  type WorkspaceContextMenu,
} from './agentSessions/AgentSessionsContextMenus';
import AgentSessionsFilterMenu from './agentSessions/AgentSessionsFilterMenu';
import AgentSessionsSessionTree from './agentSessions/AgentSessionsSessionTree';
import AgentSessionsWorkspaceGroups from './agentSessions/AgentSessionsWorkspaceGroups';
import { useAgentSessionsTreeState } from './agentSessions/useAgentSessionsTreeState';
import WorkspaceRenameDialog from './workspace/WorkspaceRenameDialog';
import {
  WORKSPACE_SECTION_RECENT_LIMIT,
  buildTimeSections,
  buildWorkspaceSections,
  reorderWorkspaceIds,
  sortSessions,
  type SessionFilterMode,
  type SessionGroupingMode,
  type SessionSortMode,
} from './agentSessions/agentSessionsUtils';

interface AgentSessionsPanelProps {
  sessions: Session[];
  currentSessionId: string;
  onSelectSession: (sessionId: string) => void;
  onRenameSession: (sessionId: string, currentTitle: string) => void;
  onDeleteSession: (sessionId: string, currentTitle: string) => void;
  onSetSessionParent: (
    workspaceId: string,
    sessionId: string,
    parentSessionId: string | null,
  ) => Promise<void>;
  onForkSessionContext: (
    workspaceId: string,
    sourceSessionId: string,
  ) => Promise<void>;
  onStatusChange: (message: string) => void;
  isOpen: boolean;
  workspaceName: string;
  gatewayWorkspaces: GatewayWorkspace[];
  activeGatewayWorkspaceId: string | null;
  sessionsByWorkspace: Map<string, Session[]>;
  workspaceSwitching: boolean;
  removingGatewayWorkspaceIds: Set<string>;
  onActivateWorkspace: (workspaceId: string) => Promise<void>;
  onRemoveWorkspace: (workspaceId: string, workspaceName: string) => void;
  onRenameWorkspace: (workspaceId: string, name: string) => Promise<string>;
  onSetWorkspaceParent: (
    workspaceId: string,
    parentWorkspaceId: string | null,
  ) => Promise<void>;
  onReorderWorkspaces: (workspaceIds: string[]) => Promise<void>;
  onCopySessionInformation: (
    workspaceId: string,
    sessionId: string,
  ) => Promise<void>;
  onCopyWorkspaceInformation: (workspaceId: string) => Promise<void>;
  onSelectWorkspaceSession: (workspaceId: string, sessionId: string) => void | Promise<void>;
  activeSession: Session | null;
  sessionAttachmentSummaries: Map<string, SessionAttachmentSummary>;
  onCreateSession: (workspaceId?: string | null) => void;
  flexRatio: number;
  customizationsCollapsed: boolean;
  customizationsHeight: number;
  onCustomizationsCollapsedChange: (collapsed: boolean) => void;
  onCustomizationsHeightChange: (height: number, commit: boolean) => void;
}

interface WorkspaceDropTarget {
  workspaceId: string;
  position: 'before' | 'after';
}

export default function AgentSessionsPanel({
  sessions,
  currentSessionId,
  onSelectSession,
  onRenameSession,
  onDeleteSession,
  onSetSessionParent,
  onForkSessionContext,
  onStatusChange,
  isOpen,
  workspaceName,
  gatewayWorkspaces,
  activeGatewayWorkspaceId,
  sessionsByWorkspace,
  workspaceSwitching,
  removingGatewayWorkspaceIds,
  onActivateWorkspace,
  onRemoveWorkspace,
  onRenameWorkspace,
  onSetWorkspaceParent,
  onReorderWorkspaces,
  onCopySessionInformation,
  onCopyWorkspaceInformation,
  onSelectWorkspaceSession,
  activeSession,
  sessionAttachmentSummaries,
  onCreateSession,
  flexRatio,
  customizationsCollapsed,
  customizationsHeight,
  onCustomizationsCollapsedChange,
  onCustomizationsHeightChange,
}: AgentSessionsPanelProps) {
  const [contextMenu, setContextMenu] = useState<SessionContextMenu | null>(null);
  const [workspaceContextMenu, setWorkspaceContextMenu] =
    useState<WorkspaceContextMenu | null>(null);
  const [renamingWorkspace, setRenamingWorkspace] =
    useState<GatewayWorkspace | null>(null);
  const [draggingWorkspaceId, setDraggingWorkspaceId] = useState<string | null>(null);
  const [workspaceDropTarget, setWorkspaceDropTarget] =
    useState<WorkspaceDropTarget | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [filterMenuOpen, setFilterMenuOpen] = useState(false);
  const [filterMode, setFilterMode] = useState<SessionFilterMode>('all');
  const [sortMode, setSortMode] = useState<SessionSortMode>('updated');
  const [groupingMode, setGroupingMode] = useState<SessionGroupingMode>('workspace');
  const [workspaceGroupCapped, setWorkspaceGroupCapped] = useState(true);
  const [collapsedSectionIds, setCollapsedSectionIds] = useState<Set<string>>(() => new Set());
  const {
    collapsedWorkspaceIds,
    collapsedSessionIds,
    expandedRootTreeIds,
    toggleWorkspace,
    expandWorkspace,
    toggleSession,
    toggleRootList,
  } = useAgentSessionsTreeState();
  const [customizationNotice, setCustomizationNotice] = useState('');
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
  const matchingSessionCount = filteredSessions.length;

  useEffect(() => {
    if (!contextMenu && !workspaceContextMenu) {
      return;
    }

    const closeMenu = () => {
      setContextMenu(null);
      setWorkspaceContextMenu(null);
    };
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
  }, [contextMenu, workspaceContextMenu]);

  useEffect(() => {
    if (!isOpen) {
      setContextMenu(null);
      setWorkspaceContextMenu(null);
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

  const openSessionMenu = (
    session: Session,
    workspaceId: string,
    x: number,
    y: number,
  ) => {
    const menuWidth = 216;
    const menuHeight = session.parent_session_id ? 224 : 196;
    setWorkspaceContextMenu(null);
    setContextMenu({
      sessionId: session.session_id,
      workspaceId,
      title: session.title || '',
      parentSessionId: session.parent_session_id ?? null,
      x: Math.max(8, Math.min(x, window.innerWidth - menuWidth - 8)),
      y: Math.max(8, Math.min(y, window.innerHeight - menuHeight - 8)),
    });
  };
  const openWorkspaceMenu = (workspace: GatewayWorkspace, x: number, y: number) => {
    const menuWidth = 216;
    const menuHeight =
      120 + (workspace.parent_workspace_id ? 28 : 0) +
      (workspace.removable ? 40 : 58);
    setContextMenu(null);
    setWorkspaceContextMenu({
      workspaceId: workspace.workspace_id,
      name: workspace.name,
      parentWorkspaceId: workspace.parent_workspace_id ?? null,
      removable: workspace.removable,
      x: Math.max(8, Math.min(x, window.innerWidth - menuWidth - 8)),
      y: Math.max(8, Math.min(y, window.innerHeight - menuHeight - 8)),
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
  const toggleWorkspaceSection = (workspaceId: string) => {
    toggleWorkspace(workspaceId);
  };
  const handleActivateWorkspace = (workspace: GatewayWorkspace) => {
    if (workspace.workspace_id === activeGatewayWorkspaceId || workspaceSwitching) {
      return;
    }
    void onActivateWorkspace(workspace.workspace_id).catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      onStatusChange(`工作区切换失败: ${message}`);
    });
  };
  const handleSelectWorkspaceSession = (workspaceId: string, sessionId: string) => {
    void Promise.resolve(onSelectWorkspaceSession(workspaceId, sessionId)).catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      onStatusChange(`切换历史会话失败: ${message}`);
    });
  };
  const handleCreateWorkspaceSession = (workspace: GatewayWorkspace) => {
    if (workspaceSwitching) {
      onStatusChange('工作区正在切换，请稍后再新建会话');
      return;
    }
    expandWorkspace(workspace.workspace_id);
    void (async () => {
      if (workspace.workspace_id !== activeGatewayWorkspaceId) {
        await onActivateWorkspace(workspace.workspace_id);
      }
      onCreateSession(workspace.workspace_id);
      onStatusChange(`已在 ${workspace.name} 新建会话`);
    })().catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      onStatusChange(`新建工作区会话失败: ${message}`);
    });
  };
  const handleWorkspaceDragStart = (
    event: React.DragEvent<HTMLElement>,
    workspaceId: string,
  ) => {
    if (workspaceSwitching) {
      event.preventDefault();
      return;
    }
    setContextMenu(null);
    setWorkspaceContextMenu(null);
    setDraggingWorkspaceId(workspaceId);
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', workspaceId);
  };
  const updateWorkspaceDropTarget = (
    event: React.DragEvent<HTMLElement>,
    workspaceId: string,
  ) => {
    if (!draggingWorkspaceId || draggingWorkspaceId === workspaceId) {
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    const rect = event.currentTarget.getBoundingClientRect();
    const position = event.clientY > rect.top + rect.height / 2 ? 'after' : 'before';
    setWorkspaceDropTarget({ workspaceId, position });
  };
  const handleWorkspaceDrop = (
    event: React.DragEvent<HTMLElement>,
    targetWorkspaceId: string,
  ) => {
    event.preventDefault();
    const sourceWorkspaceId =
      draggingWorkspaceId || event.dataTransfer.getData('text/plain');
    const target = workspaceDropTarget?.workspaceId === targetWorkspaceId
      ? workspaceDropTarget
      : { workspaceId: targetWorkspaceId, position: 'before' as const };
    setDraggingWorkspaceId(null);
    setWorkspaceDropTarget(null);
    if (!sourceWorkspaceId || sourceWorkspaceId === targetWorkspaceId) {
      return;
    }
    const workspaceIds = gatewayWorkspaces.map((workspace) => workspace.workspace_id);
    const nextWorkspaceIds = reorderWorkspaceIds(
      workspaceIds,
      sourceWorkspaceId,
      target.workspaceId,
      target.position,
    );
    void onReorderWorkspaces(nextWorkspaceIds).catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      onStatusChange(`工作区排序失败: ${message}`);
    });
  };
  const finishWorkspaceDrag = () => {
    setDraggingWorkspaceId(null);
    setWorkspaceDropTarget(null);
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
    let latestHeight = startHeight;

    const handlePointerMove = (moveEvent: PointerEvent) => {
      const deltaY = moveEvent.clientY - startY;
      latestHeight = clampCustomizationsHeight(startHeight - deltaY);
      onCustomizationsHeightChange(latestHeight, false);
    };

    const finishResize = () => {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', finishResize);
      window.removeEventListener('pointercancel', finishResize);
      document.body.classList.remove(CUSTOMIZATIONS_RESIZING_CLASS);
      cleanupCustomizationsResizeRef.current = null;
      onCustomizationsHeightChange(latestHeight, true);
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
    <aside
      className={`agent-sessions-panel${isOpen ? '' : ' preserve-mounted-hidden'}`}
      hidden={!isOpen}
      style={{ flexBasis: 0, flexGrow: flexRatio }}
    >
      <div className="agent-sessions-panel-shell">
        <header className="panel-header agent-sessions-panel-header">
          <span className="panel-title">会话</span>
          <section ref={filterControlRef} className="sessions-sidebar-actions" aria-label="会话操作">
            <button
              type="button"
              className="new-session-pill"
              onClick={() => onCreateSession()}
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
              <span className="codicon codicon-filter" aria-hidden="true" />
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
              <span className="codicon codicon-search" aria-hidden="true" />
            </button>
            {filterMenuOpen ? (
              <AgentSessionsFilterMenu
                filterMode={filterMode}
                sortMode={sortMode}
                groupingMode={groupingMode}
                workspaceGroupCapped={workspaceGroupCapped}
                onApplyFilterMode={applyFilterMode}
                onApplySortMode={applySortMode}
                onApplyGroupingMode={applyGroupingMode}
                onToggleWorkspaceGroupCapping={toggleWorkspaceGroupCapping}
                onCollapseAllSessionSections={collapseAllSessionSections}
              />
            ) : null}
          </section>
        </header>

        <div className="panel-body agent-sessions-panel-body">
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
          <section className="agent-sessions-sidebar-groups" aria-label="会话导航">
            <button type="button" className="agent-sessions-nav-row" onClick={() => showCustomizationNotice('已固定')}>
              <span className="codicon codicon-pinned agent-sessions-nav-icon" aria-hidden="true" />
              <span>已固定</span>
            </button>
            <button type="button" className="agent-sessions-nav-row" onClick={() => applyFilterMode('all', 'Chats')}>
              <span className="codicon codicon-comment-discussion agent-sessions-nav-icon" aria-hidden="true" />
              <span>Chats</span>
            </button>
            <div className="agent-sessions-no-chats">No chats</div>
          </section>

          <AgentSessionsWorkspaceGroups
            gatewayWorkspaces={gatewayWorkspaces}
            activeGatewayWorkspaceId={activeGatewayWorkspaceId}
            sessionsByWorkspace={sessionsByWorkspace}
            sortMode={sortMode}
            collapsedWorkspaceIds={collapsedWorkspaceIds}
            collapsedSessionIds={collapsedSessionIds}
            expandedRootTreeIds={expandedRootTreeIds}
            draggingWorkspaceId={draggingWorkspaceId}
            workspaceDropTarget={workspaceDropTarget}
            workspaceSwitching={workspaceSwitching}
            removingGatewayWorkspaceIds={removingGatewayWorkspaceIds}
            currentSessionId={currentSessionId}
            onToggleWorkspaceSection={toggleWorkspaceSection}
            onToggleSession={toggleSession}
            onToggleShowAllRoots={toggleRootList}
            onWorkspaceDragStart={handleWorkspaceDragStart}
            onWorkspaceDragOver={updateWorkspaceDropTarget}
            onWorkspaceDrop={handleWorkspaceDrop}
            onWorkspaceDragEnd={finishWorkspaceDrag}
            onOpenWorkspaceMenu={openWorkspaceMenu}
            onCreateWorkspaceSession={handleCreateWorkspaceSession}
            onSelectWorkspaceSession={handleSelectWorkspaceSession}
            onOpenSessionMenu={openSessionMenu}
          />

          {gatewayWorkspaces.length === 0 ? (
            <section className="agent-sessions-section agent-sessions-list-section">
            {matchingSessionCount === 0 && gatewayWorkspaces.length === 0 ? (
              <div className="empty-state small">暂无会话</div>
            ) : (
              <>
                {sessionSections.map((section) => {
                  const collapsed = collapsedSectionIds.has(section.id);
                  return (
                    <section className="agent-sessions-session-section" key={section.id}>
                      <button
                        type="button"
                        className={`agent-sessions-section-title${collapsed ? ' collapsed' : ''}`}
                        aria-expanded={!collapsed}
                        onClick={() => toggleSessionSection(section.id)}
                      >
                        <span className="agent-sessions-section-chevron" aria-hidden="true">
                          {collapsed ? '›' : '⌄'}
                        </span>
                        <span className="agent-sessions-section-label">{section.label}</span>
                      </button>
                      {!collapsed ? (
                        <>
                          <AgentSessionsSessionTree
                            sessions={section.sessions}
                            sortMode={sortMode}
                            currentSessionId={currentSessionId}
                            active
                            treeId={`section:${section.id}`}
                            collapsedSessionIds={collapsedSessionIds}
                            showAllRoots={expandedRootTreeIds.has(
                              `section:${section.id}`,
                            )}
                            onSelectSession={onSelectSession}
                            onToggleSession={toggleSession}
                            onToggleShowAllRoots={toggleRootList}
                            onOpenMenu={(session, x, y) =>
                              openSessionMenu(
                                session,
                                activeGatewayWorkspaceId ??
                                  activeSession?.workspace_id ??
                                  'ws_local',
                                x,
                                y,
                              )
                            }
                          />
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
          ) : null}
        </div>
        {!customizationsCollapsed ? (
          <button
            type="button"
            className="agent-sessions-customizations-resize-sash"
            title="拖拽调整会话列表和自定义区域大小，双击还原"
            aria-label="调整会话列表和自定义区域大小"
            onPointerDown={startCustomizationsResize}
            onDoubleClick={() =>
              onCustomizationsHeightChange(CUSTOMIZATIONS_DEFAULT_HEIGHT, true)
            }
          />
        ) : null}
        <AgentSessionsCustomizations
          collapsed={customizationsCollapsed}
          height={displayedCustomizationsHeight}
          sessionCount={sessions.length}
          notice={customizationNotice}
          onCollapsedChange={onCustomizationsCollapsedChange}
          onShowNotice={showCustomizationNotice}
        />
        <AgentSessionsContextMenus
          sessionMenu={contextMenu}
          workspaceMenu={workspaceContextMenu}
          onCloseSessionMenu={() => setContextMenu(null)}
          onCloseWorkspaceMenu={() => setWorkspaceContextMenu(null)}
          onRenameSession={onRenameSession}
          onDeleteSession={onDeleteSession}
          onUnbindSession={(sessionId, workspaceId) => {
            void onSetSessionParent(workspaceId, sessionId, null).catch(
              (error: unknown) => {
                const message = error instanceof Error ? error.message : String(error);
                onStatusChange(`解除会话绑定失败: ${message}`);
              },
            );
          }}
          onBindClipboardSession={(sessionId, parentSessionId, workspaceId) =>
            onSetSessionParent(workspaceId, sessionId, parentSessionId)
          }
          onForkSessionContext={onForkSessionContext}
          onCopySessionInformation={onCopySessionInformation}
          onRenameWorkspace={(workspaceId) => {
            const workspace = gatewayWorkspaces.find(
              (candidate) => candidate.workspace_id === workspaceId,
            );
            if (!workspace) {
              onStatusChange(`无法重命名未知工作区: ${workspaceId}`);
              return;
            }
            setRenamingWorkspace(workspace);
          }}
          onUnbindWorkspace={(workspaceId) =>
            onSetWorkspaceParent(workspaceId, null)
          }
          onBindClipboardWorkspace={(workspaceId, parentWorkspaceId) =>
            onSetWorkspaceParent(workspaceId, parentWorkspaceId)
          }
          onCopyWorkspaceInformation={onCopyWorkspaceInformation}
          onRemoveWorkspace={onRemoveWorkspace}
          onStatusChange={onStatusChange}
        />
        <WorkspaceRenameDialog
          workspace={renamingWorkspace}
          onClose={() => setRenamingWorkspace(null)}
          onSubmit={onRenameWorkspace}
        />
      </div>
    </aside>
  );
}
