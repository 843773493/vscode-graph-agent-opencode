import React, { useEffect, useMemo, useRef, useState } from 'react';
import type {
  AddManagedGatewayWorkspaceRequest,
  GatewayWorkspace,
  Session,
  WebUiSessionSidebarSettings,
} from '../types/backend';
import type { SessionAttachmentSummary } from '../types/frontend';
import type { AgentSessionsPreferences } from '../state/uiSettings/preferences';
import { stableUiSettingIds } from '../state/uiSettings/preferences';
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
import SessionResourceExplorer from './agentSessions/SessionResourceExplorer';
import { useAgentSessionsTreeState } from './agentSessions/useAgentSessionsTreeState';
import WorkspaceRenameDialog from './workspace/WorkspaceRenameDialog';
import WorkspaceAddDialog from './workspace/WorkspaceAddDialog';
import AnchoredOverlay from './AnchoredOverlay';
import WarmActionDialog from './WarmActionDialog';
import type { SessionGeneratorResourcesController } from '../hooks/sessionResourceExplorer/useSessionGeneratorResources';
import {
  WORKSPACE_SECTION_RECENT_LIMIT,
  buildTimeSections,
  buildWorkspaceSections,
  sortSessions,
  type SessionFilterMode,
  type SessionGroupingMode,
  type SessionSortMode,
} from './agentSessions/agentSessionsUtils';

function toggleSetValue(values: Set<string>, value: string): Set<string> {
  const next = new Set(values);
  if (next.has(value)) {
    next.delete(value);
  } else {
    next.add(value);
  }
  return next;
}

interface AgentSessionsPanelProps {
  apiPort: number;
  sessions: Session[];
  currentSessionId: string;
  onSelectSession: (sessionId: string) => void;
  onRenameSession: (sessionId: string, currentTitle: string, workspaceId: string) => void;
  onDeleteSession: (sessionId: string, currentTitle: string, workspaceId: string) => void;
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
  workspaceSwitching: boolean;
  onActivateWorkspace: (workspaceId: string) => Promise<void>;
  onSetWorkspaceParent: (
    workspaceId: string,
    parentWorkspaceId: string | null,
  ) => Promise<void>;
  onRefreshWorkspaceSessions: (workspaceId: string) => Promise<void>;
  onRemoveWorkspace: (workspaceId: string, workspaceName: string) => void;
  onAddWorkspace: (payload: AddManagedGatewayWorkspaceRequest) => Promise<void>;
  onOpenGatewayControl: () => void;
  onReconnectWorkspace: (workspaceId: string) => Promise<void>;
  onStartWorkspace: (workspaceId: string) => Promise<void>;
  onStopWorkspace: (workspaceId: string) => Promise<void>;
  onRenameWorkspace: (workspaceId: string, name: string) => Promise<string>;
  onCopySessionInformation: (
    workspaceId: string,
    sessionId: string,
  ) => Promise<void>;
  onCopyWorkspaceInformation: (workspaceId: string) => Promise<void>;
  onSelectWorkspaceSession: (workspaceId: string, sessionId: string) => void | Promise<void>;
  activeSession: Session | null;
  sessionAttachmentSummaries: Map<string, SessionAttachmentSummary>;
  activeJobIdsBySession: ReadonlyMap<string, string>;
  unreadSessionKeys: ReadonlySet<string>;
  onCreateSession: (workspaceId?: string | null) => Promise<void>;
  onCreateSessionInFolder: (
    workspaceId: string,
    folderId: string,
  ) => Promise<void>;
  onCreateSessionFolder: (
    workspaceId: string,
    parentNodeId: string | null,
    name: string,
  ) => Promise<void>;
  onSessionFolderDeleted: (
    workspaceId: string,
    deletedCurrentSession: boolean,
  ) => Promise<void>;
  onInvalidateSessionCatalog: (workspaceId: string) => void;
  catalogSyncKeys: ReadonlyMap<string, string>;
  catalogRefreshVersions: ReadonlyMap<string, number>;
  flexRatio: number;
  preferences: AgentSessionsPreferences;
  onPreferencesChange: (
    updater: (
      current: WebUiSessionSidebarSettings,
    ) => Partial<WebUiSessionSidebarSettings>,
  ) => void;
  customizationsCollapsed: boolean;
  customizationsHeight: number;
  onCustomizationsCollapsedChange: (collapsed: boolean) => void;
  onCustomizationsHeightChange: (height: number, commit: boolean) => void;
  generatorResources: SessionGeneratorResourcesController;
}

export default function AgentSessionsPanel({
  apiPort,
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
  workspaceSwitching,
  onActivateWorkspace,
  onSetWorkspaceParent,
  onRefreshWorkspaceSessions,
  onRemoveWorkspace,
  onAddWorkspace,
  onOpenGatewayControl,
  onReconnectWorkspace,
  onStartWorkspace,
  onStopWorkspace,
  onRenameWorkspace,
  onCopySessionInformation,
  onCopyWorkspaceInformation,
  onSelectWorkspaceSession,
  activeSession,
  sessionAttachmentSummaries,
  activeJobIdsBySession,
  unreadSessionKeys,
  onCreateSession,
  onCreateSessionInFolder,
  onCreateSessionFolder,
  onSessionFolderDeleted,
  onInvalidateSessionCatalog,
  catalogSyncKeys,
  catalogRefreshVersions,
  flexRatio,
  preferences,
  onPreferencesChange,
  customizationsCollapsed,
  customizationsHeight,
  onCustomizationsCollapsedChange,
  onCustomizationsHeightChange,
  generatorResources,
}: AgentSessionsPanelProps) {
  const [contextMenu, setContextMenu] = useState<SessionContextMenu | null>(null);
  const [workspaceContextMenu, setWorkspaceContextMenu] =
    useState<WorkspaceContextMenu | null>(null);
  const [renamingWorkspace, setRenamingWorkspace] =
    useState<GatewayWorkspace | null>(null);
  const [sessionFolderDialog, setSessionFolderDialog] = useState<{
    workspaceId: string;
    parentNodeId: string | null;
    locationName: string;
  } | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [workspaceAddOpen, setWorkspaceAddOpen] = useState(false);
  const [startingWorkspaceIds, setStartingWorkspaceIds] = useState<Set<string>>(
    new Set(),
  );
  const [searchQuery, setSearchQuery] = useState('');
  const [filterMenuOpen, setFilterMenuOpen] = useState(false);
  const filterMode = preferences.filterMode;
  const sortMode = preferences.sortMode;
  const groupingMode = preferences.groupingMode;
  const workspaceGroupCapped = preferences.workspaceGroupCapped;
  const collapsedSectionIds = useMemo(
    () => new Set(preferences.collapsedSectionIds),
    [preferences.collapsedSectionIds],
  );
  const {
    collapsedSessionIds,
    expandedRootTreeIds,
    toggleSession,
    toggleRootList,
  } = useAgentSessionsTreeState({
    preferences,
    onPreferencesChange,
  });
  const [customizationNotice, setCustomizationNotice] = useState('');
  const filterButtonRef = useRef<HTMLButtonElement | null>(null);
  const cleanupCustomizationsResizeRef = useRef<(() => void) | null>(null);
  const handleStartWorkspace = async (workspaceId: string) => {
    setStartingWorkspaceIds((previous) => new Set(previous).add(workspaceId));
    try {
      await onStartWorkspace(workspaceId);
      try {
        await onRefreshWorkspaceSessions(workspaceId);
      } catch (error) {
        onStatusChange(
          `工作区已启动，但刷新会话列表失败: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
      onInvalidateSessionCatalog(workspaceId);
    } finally {
      setStartingWorkspaceIds((previous) => {
        const next = new Set(previous);
        next.delete(workspaceId);
        return next;
      });
    }
  };
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

  const openSessionMenu = (
    session: Session,
    workspaceId: string,
    x: number,
    y: number,
  ) => {
    setWorkspaceContextMenu(null);
    setContextMenu({
      sessionId: session.session_id,
      workspaceId,
      title: session.title || '',
      parentSessionId: session.parent_session_id ?? null,
      x,
      y,
    });
  };
  const openWorkspaceMenu = (workspace: GatewayWorkspace, x: number, y: number) => {
    setContextMenu(null);
    setWorkspaceContextMenu({
      workspaceId: workspace.workspace_id,
      name: workspace.name,
      parentWorkspaceId: workspace.parent_workspace_id ?? null,
      removable: workspace.removable,
      managed: workspace.managed,
      systemDefault: workspace.system_default,
      status: workspace.status,
      x,
      y,
    });
  };
  const applyFilterMode = (mode: SessionFilterMode, label: string) => {
    onPreferencesChange(() => ({ filter_mode: mode }));
    setFilterMenuOpen(false);
    onStatusChange(`已筛选会话: ${label}`);
  };
  const applySortMode = (mode: SessionSortMode, label: string) => {
    onPreferencesChange(() => ({ sort_mode: mode }));
    setFilterMenuOpen(false);
    onStatusChange(`已排序会话: ${label}`);
  };
  const applyGroupingMode = (mode: SessionGroupingMode, label: string) => {
    onPreferencesChange(() => ({ grouping_mode: mode }));
    setFilterMenuOpen(false);
    onStatusChange(`已分组会话: ${label}`);
  };
  const toggleWorkspaceGroupCapping = (capped: boolean) => {
    onPreferencesChange(() => ({ workspace_group_capped: capped }));
    setFilterMenuOpen(false);
    onStatusChange(capped ? '仅显示最近工作区会话' : '显示全部工作区会话');
  };
  const toggleSessionSection = (sectionId: string) => {
    onPreferencesChange((current) => ({
      collapsed_section_ids: stableUiSettingIds(
        toggleSetValue(new Set(current.collapsed_section_ids), sectionId),
      ),
    }));
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
  const collapseAllSessionSections = () => {
    onPreferencesChange(() => ({
      collapsed_section_ids: sessionSections.map((section) => section.id),
    }));
    setFilterMenuOpen(false);
    onStatusChange('已折叠全部会话分组');
  };
  const showCustomizationNotice = (label: string) => {
    const message = `${label} 需要桌面运行时提供，当前 Web 端暂未接入`;
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
      data-bt-surface="chrome"
    >
      <div className="agent-sessions-panel-shell">
        <header className="panel-header agent-sessions-panel-header">
          <span className="panel-title">会话</span>
          <section className="sessions-sidebar-actions" aria-label="会话操作">
            <button
              type="button"
              className="new-session-pill"
              onClick={() => {
                void onCreateSession().catch((error: unknown) => {
                  const message = error instanceof Error ? error.message : String(error);
                  onStatusChange(`创建会话失败: ${message}`);
                });
              }}
              title="新建会话"
            >
              <span>新</span>
              <kbd>Ctrl+N</kbd>
            </button>
            <button
              ref={filterButtonRef}
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
            <AnchoredOverlay
              open={filterMenuOpen}
              anchorRef={filterButtonRef}
              placement="bottom-end"
              onClose={() => setFilterMenuOpen(false)}
            >
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
            </AnchoredOverlay>
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

          {gatewayWorkspaces.length > 0 ? (
            <SessionResourceExplorer
              apiPort={apiPort}
              workspaces={gatewayWorkspaces}
              activeWorkspaceId={activeGatewayWorkspaceId}
              currentSessionId={currentSessionId}
              searchOpen={searchOpen}
              searchQuery={searchQuery}
              workspaceSwitching={workspaceSwitching}
              startingWorkspaceIds={startingWorkspaceIds}
              onActivateWorkspace={onActivateWorkspace}
              onSetWorkspaceParent={onSetWorkspaceParent}
              onRefreshWorkspaceSessions={onRefreshWorkspaceSessions}
              onCreateSessionInFolder={onCreateSessionInFolder}
              onSessionFolderDeleted={onSessionFolderDeleted}
              catalogSyncKeys={catalogSyncKeys}
              catalogRefreshVersions={catalogRefreshVersions}
              onSelectSession={onSelectWorkspaceSession}
              onStatusChange={onStatusChange}
              onOpenWorkspaceMenu={openWorkspaceMenu}
              onOpenSessionMenu={openSessionMenu}
              activeJobIdsBySession={activeJobIdsBySession}
              unreadSessionKeys={unreadSessionKeys}
              onRequestAddWorkspace={() => setWorkspaceAddOpen(true)}
              onOpenConnectionManager={onOpenGatewayControl}
              onReconnectWorkspace={onReconnectWorkspace}
              onStartWorkspace={handleStartWorkspace}
              generatorResources={generatorResources}
            />
          ) : null}

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
                        <span
                          className={`codicon agent-sessions-section-chevron codicon-chevron-${collapsed ? "right" : "down"}`}
                          aria-hidden="true"
                        />
                        <span className="agent-sessions-section-label">{section.label}</span>
                      </button>
                      {!collapsed ? (
                        <>
                          <AgentSessionsSessionTree
                            sessions={section.sessions}
                            sortMode={sortMode}
                            currentSessionId={currentSessionId}
                            active
                            workspaceId={
                              activeGatewayWorkspaceId
                              ?? activeSession?.workspace_id
                              ?? 'ws_local'
                            }
                            activeJobIdsBySession={activeJobIdsBySession}
                            unreadSessionKeys={unreadSessionKeys}
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
          onCreateWorkspaceSession={async (workspaceId, targetWorkspaceName) => {
            if (workspaceId !== activeGatewayWorkspaceId) {
              await onActivateWorkspace(workspaceId);
            }
            await onCreateSession(workspaceId);
            onStatusChange(`已在 ${targetWorkspaceName} 创建会话`);
          }}
          onRequestCreateSessionFolder={(workspaceId, parentNodeId, locationName) => {
            setSessionFolderDialog({ workspaceId, parentNodeId, locationName });
          }}
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
          onCopyWorkspaceInformation={onCopyWorkspaceInformation}
          onRemoveWorkspace={onRemoveWorkspace}
          onStartWorkspace={handleStartWorkspace}
          onStopWorkspace={onStopWorkspace}
          startingWorkspaceIds={startingWorkspaceIds}
          onStatusChange={onStatusChange}
        />
        <WorkspaceRenameDialog
          workspace={renamingWorkspace}
          onClose={() => setRenamingWorkspace(null)}
          onSubmit={onRenameWorkspace}
        />
        <WorkspaceAddDialog
          open={workspaceAddOpen}
          apiPort={apiPort}
          workspaces={gatewayWorkspaces}
          onClose={() => setWorkspaceAddOpen(false)}
          onOpenConnectionManager={onOpenGatewayControl}
          onAdd={onAddWorkspace}
        />
        <WarmActionDialog
          open={sessionFolderDialog !== null}
          title={sessionFolderDialog?.parentNodeId ? "新建子文件夹" : "新建会话文件夹"}
          description={sessionFolderDialog
            ? `创建位置：${sessionFolderDialog.locationName}`
            : undefined}
          inputLabel="文件夹名称"
          initialValue="新建文件夹"
          confirmText="创建"
          onClose={() => setSessionFolderDialog(null)}
          onConfirm={async (name) => {
            if (!sessionFolderDialog) {
              throw new Error("会话文件夹创建目标已失效");
            }
            if (sessionFolderDialog.workspaceId !== activeGatewayWorkspaceId) {
              await onActivateWorkspace(sessionFolderDialog.workspaceId);
            }
            await onCreateSessionFolder(
              sessionFolderDialog.workspaceId,
              sessionFolderDialog.parentNodeId,
              name,
            );
            onStatusChange(`已创建会话文件夹 ${name}`);
          }}
        />
      </div>
    </aside>
  );
}
