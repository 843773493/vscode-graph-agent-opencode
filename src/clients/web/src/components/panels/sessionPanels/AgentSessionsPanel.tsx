import { formatNumber, t } from '../../../../../shared/core/i18n/index.js';
import { gatewayWorkspaceKey, nullableGatewayWorkspaceKey } from "../../../api/workspaceIdentity";
import React, { useEffect, useMemo, useRef, useState } from 'react';
import type {
  AddManagedGatewayWorkspaceRequest,
  GatewayWorkspace,
  Session,
  WebUiSessionSidebarSettings,
} from '../../../types/backend';
import type { SessionAttachmentSummary } from '../../../types/frontend';
import { sessionScopeKey } from '../../../state/session/sessionScope';
import type { AgentSessionsPreferences } from '../../../state/uiSettings/preferences';
import {
  stableUiSettingIds,
  toggleUiSettingId,
} from '../../../state/uiSettings/preferences';
import AgentSessionsContextMenus, {
  type SessionContextMenu,
  type WorkspaceContextMenu,
} from '../../agentSessions/AgentSessionsContextMenus';
import AgentSessionsFilterMenu from '../../agentSessions/AgentSessionsFilterMenu';
import AgentSessionsSessionTree from '../../agentSessions/AgentSessionsSessionTree';
import { useAgentSessionsTreeState } from '../../agentSessions/useAgentSessionsTreeState';
import WorkspaceRenameDialog from '../../workspace/WorkspaceRenameDialog';
import WorkspaceDetailsDialog, { type WorkspaceDetailsTarget } from '../../workspace/WorkspaceDetailsDialog';
import WorkspaceAddDialog from '../../workspace/WorkspaceAddDialog';
import AnchoredOverlay from '../../overlays/AnchoredOverlay';
import WarmActionDialog from '../../overlays/WarmActionDialog';
import type {
  SessionCatalogOutboxRuntime,
  SessionCatalogOutboxSnapshot,
} from '../../../hooks/session/sessionCatalogOutboxRuntime';
import {
  WORKSPACE_SECTION_RECENT_LIMIT,
  buildTimeSections,
  buildWorkspaceSections,
  sortSessions,
  type SessionFilterMode,
  type SessionGroupingMode,
  type SessionSortMode,
} from '../../agentSessions/agentSessionsUtils';
import { errorMessage, isErrorReported } from "../../../utils/errorMessage";
import type { CatalogRevealRequest } from '../../../hooks/sessionResourceExplorer/useCatalogReveal';

const SessionResourceExplorer = React.lazy(
  () => import('../../agentSessions/SessionResourceExplorer'),
);

interface AgentSessionsPanelProps {
  renderSessionThreads?: (workspaceId: string, session: Session) => React.ReactNode;
  catalogReveal?: CatalogRevealRequest;
  apiPort: number;
  sessions: Session[];
  currentSessionId: string;
  currentSessionWorkspaceId: string | null;
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
  onError: (message: string) => void;
  onOpenDiagnostic: (title: string, detail: string) => void;
  isOpen: boolean;
  workspaceName: string;
  gatewayWorkspaces: GatewayWorkspace[];
  gatewayWorkspacesStale: boolean;
  activeGatewayWorkspaceId: string | null;
  removingGatewayWorkspaceIds: ReadonlySet<string>;
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
  workspaceDetails: WorkspaceDetailsTarget | null;
  onCloseWorkspaceDetails: () => void;
  onOpenWorkspaceDetails: (workspaceKey: string) => Promise<void>;
  onOpenWorkspaceSettings: (workspace: GatewayWorkspace, connectionId?: string) => Promise<void>;
  onOpenWorkspaceLogs: (workspace: GatewayWorkspace, connectionId?: string) => Promise<void>;
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
  onPinSession?: (workspaceId: string, sessionId: string) => void;
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
  catalogRefreshVersions: ReadonlyMap<string, number>;
  sessionCatalogOutboxRuntime?: SessionCatalogOutboxRuntime | null;
  sessionCatalogOutboxSnapshot?: SessionCatalogOutboxSnapshot;

  flexRatio: number;
  preferences: AgentSessionsPreferences;
  onPreferencesChange: (
    updater: (
      current: WebUiSessionSidebarSettings,
    ) => Partial<WebUiSessionSidebarSettings>,
  ) => void;
}

export default function AgentSessionsPanel({
  renderSessionThreads,
  catalogReveal,
  apiPort,
  sessions,
  currentSessionId,
  currentSessionWorkspaceId,
  onSelectSession,
  onRenameSession,
  onDeleteSession,
  onSetSessionParent,
  onForkSessionContext,
  onStatusChange,
  onError,
  onOpenDiagnostic,
  isOpen,
  workspaceName,
  gatewayWorkspaces,
  gatewayWorkspacesStale,
  activeGatewayWorkspaceId,
  removingGatewayWorkspaceIds,
  workspaceSwitching,
  onActivateWorkspace,
  onSetWorkspaceParent,
  onRefreshWorkspaceSessions,
  onRemoveWorkspace,
  onAddWorkspace,
  onOpenGatewayControl,
  workspaceDetails,
  onCloseWorkspaceDetails,
  onOpenWorkspaceDetails,
  onOpenWorkspaceSettings,
  onOpenWorkspaceLogs,
  onReconnectWorkspace,
  onStartWorkspace,
  onStopWorkspace,
  onRenameWorkspace,
  onCopySessionInformation,
  onCopyWorkspaceInformation,
  onSelectWorkspaceSession,
  onPinSession,
  activeSession,
  sessionAttachmentSummaries,
  activeJobIdsBySession,
  unreadSessionKeys,
  onCreateSession,
  onCreateSessionInFolder,
  onCreateSessionFolder,
  onSessionFolderDeleted,
  onInvalidateSessionCatalog,
  catalogRefreshVersions,
  sessionCatalogOutboxRuntime,
  sessionCatalogOutboxSnapshot,

  flexRatio,
  preferences,
  onPreferencesChange,
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
  useEffect(() => {
    if (catalogReveal) { setSearchOpen(false); setSearchQuery(''); }
  }, [catalogReveal]);
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
  const filterButtonRef = useRef<HTMLButtonElement | null>(null);
  const handleStartWorkspace = async (workspaceId: string) => {
    setStartingWorkspaceIds((previous) => new Set(previous).add(workspaceId));
    try {
      await onStartWorkspace(workspaceId);
      try {
        await onRefreshWorkspaceSessions(workspaceId);
      } catch (error) {
        onError(
          t('navigation.workspace.startedRefreshFailed', workspaceId, errorMessage(error)),
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
        !sessionAttachmentSummaries.get(sessionScopeKey(activeGatewayWorkspaceId, session.session_id))?.count
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
    activeGatewayWorkspaceId,
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
      workspaceId: gatewayWorkspaceKey(workspace),
      name: workspace.name,
      parentWorkspaceId: nullableGatewayWorkspaceKey(workspace.parent_workspace),
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
    onStatusChange(t('navigation.filter.applied', label));
  };
  const applySortMode = (mode: SessionSortMode, label: string) => {
    onPreferencesChange(() => ({ sort_mode: mode }));
    setFilterMenuOpen(false);
    onStatusChange(t('navigation.filter.sorted', label));
  };
  const applyGroupingMode = (mode: SessionGroupingMode, label: string) => {
    onPreferencesChange(() => ({ grouping_mode: mode }));
    setFilterMenuOpen(false);
    onStatusChange(t('navigation.filter.grouped', label));
  };
  const toggleWorkspaceGroupCapping = (capped: boolean) => {
    onPreferencesChange(() => ({ workspace_group_capped: capped }));
    setFilterMenuOpen(false);
    onStatusChange(capped ? t('navigation.filter.recentWorkspaceSessions') : t('navigation.filter.allWorkspaceSessions'));
  };
  const toggleSessionSection = (sectionId: string) => {
    onPreferencesChange((current) => ({
      collapsed_section_ids: stableUiSettingIds(
        toggleUiSettingId(current.collapsed_section_ids, sectionId),
      ),
    }));
  };
  const collapseAllSessionSections = () => {
    onPreferencesChange(() => ({
      collapsed_section_ids: sessionSections.map((section) => section.id),
    }));
    setFilterMenuOpen(false);
    onStatusChange(t('navigation.filter.allCollapsed'));
  };
  return (
    <aside
      className={`agent-sessions-panel${isOpen ? '' : ' preserve-mounted-hidden'}`}
      hidden={!isOpen}
      style={{ flexBasis: 0, flexGrow: flexRatio }}
      data-bt-surface="chrome"
    >
      <div className="agent-sessions-panel-shell">
        <header className="panel-header agent-sessions-panel-header">
          <span className="panel-title">{t('navigation.sessions.title')}</span>
          <section className="sessions-sidebar-actions" aria-label={t('navigation.sessions.actions')}>
            <button
              type="button"
              className="new-session-pill"
              onClick={() => {
                void onCreateSession().catch((error: unknown) => {
                  if (isErrorReported(error)) return;
                  const message = errorMessage(error);
                  onError(t('navigation.sessions.createError', message));
                });
              }}
              title={t('navigation.sessions.new')}
            >
              <span>{t('navigation.sessions.newShort')}</span>
              <kbd>Ctrl+N</kbd>
            </button>
            <button
              ref={filterButtonRef}
              type="button"
              className={`sidebar-icon-button${filterMenuOpen ? ' active' : ''}`}
              title={t('navigation.filter.sessions')}
              aria-label={t('navigation.filter.sessions')}
              aria-haspopup="menu"
              aria-expanded={filterMenuOpen}
              onClick={() => setFilterMenuOpen((open) => !open)}
            >
              <span className="codicon codicon-filter" aria-hidden="true" />
            </button>
            <button
              type="button"
              className={`sidebar-icon-button${searchOpen ? ' active' : ''}`}
              title={t('navigation.search.sessions')}
              aria-label={t('navigation.search.sessions')}
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
              <span>{t('navigation.search.findSessions')}</span>
              <input
                autoFocus
                type="search"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
                placeholder={t('navigation.search.placeholder')}
              />
            </label>
          ) : null}
          <section className="agent-sessions-sidebar-groups" aria-label={t('navigation.sessions.navigation')}>
            <button
              type="button"
              className="agent-sessions-nav-row"
              aria-disabled="true"
              title={t('navigation.sessions.pinnedUnavailable')}
              onClick={() => onStatusChange(t('navigation.sessions.pinnedUnavailable'))}
            >
              <span className="codicon codicon-pinned agent-sessions-nav-icon" aria-hidden="true" />
              <span>{t('navigation.sessions.pinned')}</span>
              <span className="agent-sessions-nav-unavailable">{t('navigation.sessions.unavailable')}</span>
            </button>
            <button type="button" className="agent-sessions-nav-row" onClick={() => applyFilterMode('all', t('navigation.sessions.chats'))}>
              <span className="codicon codicon-comment-discussion agent-sessions-nav-icon" aria-hidden="true" />
              <span>{t('navigation.sessions.chats')}</span>
            </button>
            {gatewayWorkspaces.length === 0 && matchingSessionCount === 0 ? (
              <div className="agent-sessions-no-chats">{t('navigation.sessions.noChats')}</div>
            ) : null}
          </section>

          {gatewayWorkspacesStale && gatewayWorkspaces.length > 0 ? (
            // 刷新失败时保留旧列表仅作过渡展示，必须显式告知「这份结构可能已过期」，
            // 不能让用户把陈旧的工作区结构当成当前权威数据。
            <div className="agent-sessions-stale-notice" role="alert">
              <span className="codicon codicon-warning" aria-hidden="true" />
              <span>{t('navigation.workspace.staleStructure')}</span>
            </div>
          ) : null}

          {gatewayWorkspaces.length > 0 ? (
            <React.Suspense fallback={null}>
              <SessionResourceExplorer
                renderSessionThreads={renderSessionThreads}
                catalogReveal={catalogReveal}
                apiPort={apiPort}
                workspaces={gatewayWorkspaces}
                activeWorkspaceId={activeGatewayWorkspaceId}
                currentSessionId={currentSessionId}
                currentSessionWorkspaceId={currentSessionWorkspaceId}
                searchOpen={searchOpen}
                searchQuery={searchQuery}
                workspaceSwitching={workspaceSwitching}
                startingWorkspaceIds={startingWorkspaceIds}
                removingWorkspaceIds={removingGatewayWorkspaceIds}
                onActivateWorkspace={onActivateWorkspace}
                onSetWorkspaceParent={onSetWorkspaceParent}
                onRefreshWorkspaceSessions={onRefreshWorkspaceSessions}
                onCreateSessionInFolder={onCreateSessionInFolder}
                onSessionFolderDeleted={onSessionFolderDeleted}
                catalogRefreshVersions={catalogRefreshVersions}
                sessionCatalogOutboxRuntime={sessionCatalogOutboxRuntime}
                sessionCatalogOutboxSnapshot={sessionCatalogOutboxSnapshot}
                onSelectSession={onSelectWorkspaceSession}
                onCloseSearch={() => { setSearchOpen(false); setSearchQuery(''); }}
                onPinSession={onPinSession}
                onStatusChange={onStatusChange}
                onOpenDiagnostic={onOpenDiagnostic}
                onOpenWorkspaceMenu={openWorkspaceMenu}
                onOpenSessionMenu={openSessionMenu}
                activeJobIdsBySession={activeJobIdsBySession}
                unreadSessionKeys={unreadSessionKeys}
                onRequestAddWorkspace={() => setWorkspaceAddOpen(true)}
                onOpenConnectionManager={onOpenGatewayControl}
                onReconnectWorkspace={onReconnectWorkspace}
                onStartWorkspace={handleStartWorkspace}
              />
            </React.Suspense>
          ) : null}

          {gatewayWorkspaces.length === 0 ? (
            <section className="agent-sessions-section agent-sessions-list-section">
            {matchingSessionCount === 0 && gatewayWorkspaces.length === 0 ? (
              <div className="empty-state small">{t('navigation.sessions.empty')}</div>
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
                                activeGatewayWorkspaceId ?? (() => { throw new Error(t('navigation.sessions.menuOwnerMissing')); })(),
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
                              {t('navigation.sessions.showAllRemaining', formatNumber(section.showMoreCount))}
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
                              {t('navigation.filter.recentOnly')}
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
                if (isErrorReported(error)) return;
                const message = errorMessage(error);
                onError(t('navigation.sessions.unbindFailed', workspaceId, sessionId, message));
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
            onStatusChange(t('navigation.sessions.createdInWorkspace', targetWorkspaceName));
          }}
          onRequestCreateSessionFolder={(workspaceId, parentNodeId, locationName) => {
            setSessionFolderDialog({ workspaceId, parentNodeId, locationName });
          }}
          onCopySessionInformation={onCopySessionInformation}
          onRenameWorkspace={(workspaceId) => {
            const workspace = gatewayWorkspaces.find(
              (candidate) => gatewayWorkspaceKey(candidate) === workspaceId,
            );
            if (!workspace) {
              onError(t('navigation.workspace.renameUnknown', workspaceId));
              return;
            }
            setRenamingWorkspace(workspace);
          }}
          onCopyWorkspaceInformation={onCopyWorkspaceInformation}
          onOpenWorkspaceDetails={onOpenWorkspaceDetails}
          onRemoveWorkspace={onRemoveWorkspace}
          onStartWorkspace={handleStartWorkspace}
          onStopWorkspace={onStopWorkspace}
          startingWorkspaceIds={startingWorkspaceIds}
          onStatusChange={onStatusChange}
          onError={onError}
        />
        <WorkspaceRenameDialog
          workspace={renamingWorkspace}
          onClose={() => setRenamingWorkspace(null)}
          onSubmit={onRenameWorkspace}
        />
        {workspaceDetails ? <WorkspaceDetailsDialog key={workspaceDetails.workspaceKey} target={workspaceDetails}
          workspaces={gatewayWorkspaces} onClose={onCloseWorkspaceDetails} onOpen={onActivateWorkspace}
          onRename={setRenamingWorkspace} onRemove={onRemoveWorkspace}
          onOpenSettings={onOpenWorkspaceSettings} onOpenLogs={onOpenWorkspaceLogs} /> : null}
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
          title={sessionFolderDialog?.parentNodeId ? t('navigation.folder.newChild') : t('navigation.folder.newSessionFolder')}
          description={sessionFolderDialog
            ? t('navigation.folder.createLocation', sessionFolderDialog.locationName)
            : undefined}
          inputLabel={t('navigation.folder.name')}
          initialValue={t('navigation.folder.newName')}
          confirmText={t('navigation.create')}
          onClose={() => setSessionFolderDialog(null)}
          onConfirm={async (name) => {
            if (!sessionFolderDialog) {
              throw new Error(t('navigation.folder.createTargetExpired'));
            }
            if (sessionFolderDialog.workspaceId !== activeGatewayWorkspaceId) {
              await onActivateWorkspace(sessionFolderDialog.workspaceId);
            }
            await onCreateSessionFolder(
              sessionFolderDialog.workspaceId,
              sessionFolderDialog.parentNodeId,
              name,
            );
            onStatusChange(t('navigation.folder.createPending', name));
          }}
        />
      </div>
    </aside>
  );
}
