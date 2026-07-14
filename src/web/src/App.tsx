import AgentStatePanel from "./components/AgentStatePanel";
import BootstrapState from "./components/BootstrapState";
import ChatPanel from "./components/ChatPanel";
import Composer from "./components/composer/Composer";
import EventQueuePanel from "./components/EventQueuePanel";
import AgentSessionsPanel from "./components/AgentSessionsPanel";
import RequestLogPanel from "./components/RequestLogPanel";
import ResourcePanel from "./components/ResourcePanel";
import SessionNameDialog from "./components/SessionNameDialog";
import Toolbar from "./components/Toolbar";
import WorkspaceFilePreviewArea from "./components/workspace/WorkspaceFilePreviewArea";
import { WorkspaceFileReferenceProvider } from "./components/workspace/WorkspaceFileReferenceContext";
import WorkspaceAuxiliaryPanel, {
  type WorkspaceAuxiliaryTab,
} from "./components/workspace/WorkspaceAuxiliaryPanel";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { DEFAULT_BACKEND_PORT, getSessionChangesets } from "./api";
import {
  FRONTEND_EVENT_QUEUE_LIMIT,
  getConversationsForSession,
  useAppState,
} from "./hooks";
import { useWorkspacePreviewTabs } from "./hooks/useWorkspacePreviewTabs";
import {
  DEFAULT_MAIN_AREA_RATIOS,
  LAYOUT_RESIZING_CLASS,
  defaultAuxiliaryVisible,
  resizeAdjacentMainAreas,
  resolveMainAreaRatios,
  type MainAreaKey,
  type LayoutResizeTarget,
} from "./layout/workbenchLayout";
import { sessionScopeKey } from "./state/sessionScope";
import type {
  SessionChangesSummary,
  SessionFileChange,
  WebUiMainAreaRatios,
} from "./types/backend";

type SessionNameDialogState = { sessionId: string; initialTitle: string };

export default function AppShell() {
  const {
    state,
    selectSession,
    selectWorkspaceSession,
    startNewSessionDraft,
    forkSessionContext,
    renameSession,
    setSessionParent,
    deleteSession,
    refreshSessionChanges,
    refreshSessionResources,
    reviewSessionChangeFile,
    controlSessionResource,
    switchContentView,
    activateGatewayWorkspace,
    removeGatewayWorkspace,
    reorderGatewayWorkspaces,
    updateUiSettings,
    setStatus,
  } = useAppState();
  const [nameDialog, setNameDialog] = useState<SessionNameDialogState | null>(null);
  const [nameDialogSubmitting, setNameDialogSubmitting] = useState(false);
  const [nameDialogError, setNameDialogError] = useState<string | null>(null);
  const [auxiliaryTab, setAuxiliaryTab] = useState<WorkspaceAuxiliaryTab>("changes");
  const [auxiliaryVisible, setAuxiliaryVisible] = useState(
    () => state.uiSettings.layout.auxiliary_visible ?? defaultAuxiliaryVisible(),
  );
  const [fileTreeSearchOpen, setFileTreeSearchOpen] = useState(false);
  const [fileTreeCollapseVersion, setFileTreeCollapseVersion] = useState(0);
  const [mainAreaRatios, setMainAreaRatios] = useState(() =>
    resolveMainAreaRatios(state.uiSettings.layout.main_area_ratios),
  );
  const [customizationsCollapsed, setCustomizationsCollapsed] = useState(
    () => state.uiSettings.layout.customizations_collapsed ?? false,
  );
  const [customizationsHeight, setCustomizationsHeight] = useState(() =>
    Math.min(
      420,
      Math.max(
        129,
        state.uiSettings.layout.customizations_height ?? 286,
      ),
    ),
  );
  const [defaultViewChangesHint, setDefaultViewChangesHint] = useState<{
    sessionId: string;
    summary: SessionChangesSummary;
  } | null>(null);
  const [defaultViewChangesLoading, setDefaultViewChangesLoading] = useState(false);
  const lastOpenedChangesPreviewKeyRef = useRef<string | null>(null);
  const cleanupLayoutResizeRef = useRef<(() => void) | null>(null);
  const activeSession = state.currentSession;
  const activeSessionWorkspaceId =
    state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId;
  const activeSessionCacheKey =
    activeSession && activeSessionWorkspaceId
      ? sessionScopeKey(activeSessionWorkspaceId, activeSession.session_id)
      : activeSession?.session_id ?? null;

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }

    const mediaQuery = window.matchMedia("(max-width: 900px)");
    const syncAuxiliaryVisibility = () => {
      if (mediaQuery.matches) {
        setAuxiliaryVisible(false);
      }
    };

    syncAuxiliaryVisibility();
    mediaQuery.addEventListener("change", syncAuxiliaryVisibility);
    return () => {
      mediaQuery.removeEventListener("change", syncAuxiliaryVisibility);
    };
  }, []);

  useEffect(() => {
    const layout = state.uiSettings.layout;
    if (typeof layout.auxiliary_visible === "boolean") {
      setAuxiliaryVisible(layout.auxiliary_visible);
    }
    setMainAreaRatios(resolveMainAreaRatios(layout.main_area_ratios));
    if (typeof layout.customizations_collapsed === "boolean") {
      setCustomizationsCollapsed(layout.customizations_collapsed);
    }
    if (typeof layout.customizations_height === "number") {
      setCustomizationsHeight(
        Math.min(420, Math.max(129, layout.customizations_height)),
      );
    }
  }, [state.uiSettings]);

  useEffect(() => {
    return () => {
      cleanupLayoutResizeRef.current?.();
    };
  }, []);

  const conversations = useMemo(
    () => activeSession
      ? getConversationsForSession(
          activeSession.session_id,
          state,
          activeSessionCacheKey ?? activeSession.session_id,
        )
      : [],
    [
      activeSession,
      activeSessionCacheKey,
      state.messages,
      state.pendingConversations,
      state.traceEvents,
    ],
  );
  const receivedEvents = activeSession
    ? (state.eventQueuesBySession.get(activeSessionCacheKey ?? activeSession.session_id) ?? [])
    : [];
  const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
  const sortedSessions = useMemo(
    () => [...state.sessions].sort(
      (a, b) =>
        new Date(b.updated_at || b.created_at || "").getTime() -
        new Date(a.updated_at || a.created_at || "").getTime(),
    ),
    [state.sessions],
  );
  const persistLayoutSettings = useCallback(
    (layout: Parameters<typeof updateUiSettings>[0]["layout"]) => {
      void updateUiSettings({ layout }).catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        setStatus(`保存页面设置失败: ${message}`);
      });
    },
    [setStatus, updateUiSettings],
  );
  const agentSessionsVisible = state.agentSessionsPanelOpen;
  const workspacePreview = useWorkspacePreviewTabs({
    apiPort: resolvedApiPort,
    workspaceId: activeSessionWorkspaceId,
    workspaceRoot: state.workspaceRoot ?? "",
    settingsLoaded: state.uiSettingsLoaded,
    restoredLayout: state.uiSettings.layout,
    onPersistLayout: persistLayoutSettings,
    onStatusChange: setStatus,
  });
  const previewVisible = workspacePreview.visible;
  const previewMaximized = workspacePreview.maximized;
  const previewTabs = workspacePreview.tabs;
  const activePreviewPath = workspacePreview.activePath;
  const previewLoadingPath = workspacePreview.loadingPath;
  const previewError = workspacePreview.error;

  const openSessionChangeInPreview = (file: SessionFileChange) => {
    if (!state.activeChangeset) {
      return;
    }
    const key = `${state.activeChangeset.changeset_id}:${file.file_path}:${file.reviewed}`;
    lastOpenedChangesPreviewKeyRef.current = key;
    workspacePreview.openSessionChangePreview(state.activeChangeset, file);
  };

  useEffect(() => {
    if (!activeSession || state.contentView !== "default") {
      setDefaultViewChangesHint(null);
      setDefaultViewChangesLoading(false);
      return;
    }

    if (auxiliaryVisible && auxiliaryTab === "changes") {
      if (state.activeChangeset?.session_id === activeSession.session_id) {
        setDefaultViewChangesHint({
          sessionId: activeSession.session_id,
          summary: state.activeChangeset.summary,
        });
        setDefaultViewChangesLoading(false);
      } else {
        setDefaultViewChangesLoading(state.sessionChangesLoading);
      }
      return;
    }

    let cancelled = false;
    setDefaultViewChangesLoading(true);
    void getSessionChangesets(
      resolvedApiPort,
      activeSession.session_id,
      activeSessionWorkspaceId,
    )
      .then((list) => {
        if (cancelled) {
          return;
        }
        const summary =
          list.items.find((item) => item.is_default)?.summary ??
          list.items[0]?.summary ??
          { files: 0, additions: 0, deletions: 0 };
        setDefaultViewChangesHint({
          sessionId: activeSession.session_id,
          summary,
        });
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        const message = error instanceof Error ? error.message : String(error);
        setDefaultViewChangesHint(null);
        setStatus(`会话文件变更提示加载失败: ${message}`);
      })
      .finally(() => {
        if (!cancelled) {
          setDefaultViewChangesLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [
    activeSession,
    activeSessionWorkspaceId,
    auxiliaryTab,
    auxiliaryVisible,
    resolvedApiPort,
    setStatus,
    state.activeChangeset,
    state.contentView,
    state.sessionChangesLoading,
  ]);

  useEffect(() => {
    if (state.contentView !== "changes") {
      return;
    }

    setAuxiliaryVisible(true);
    setAuxiliaryTab("changes");
  }, [state.contentView]);

  useEffect(() => {
    if (state.contentView !== "changes") {
      return;
    }

    if (!state.activeChangeset || state.activeChangeset.files.length === 0) {
      return;
    }

    const activeDiffFile = state.activeChangeset.files.find(
      (file) =>
        activePreviewPath ===
        `session-diff://${state.activeChangeset?.changeset_id}/${encodeURIComponent(file.file_path)}`,
    );
    const targetFile = activeDiffFile ?? state.activeChangeset.files[0];
    const key = `${state.activeChangeset.changeset_id}:${targetFile.file_path}:${targetFile.reviewed}`;
    if (lastOpenedChangesPreviewKeyRef.current === key) {
      return;
    }
    lastOpenedChangesPreviewKeyRef.current = key;
    workspacePreview.openSessionChangePreview(state.activeChangeset, targetFile);
  }, [
    activePreviewPath,
    state.activeChangeset,
    state.contentView,
    workspacePreview.openSessionChangePreview,
  ]);

  useEffect(() => {
    if (
      !activeSession ||
      !auxiliaryVisible ||
      auxiliaryTab !== "changes" ||
      state.contentView === "changes"
    ) {
      return;
    }
    if (state.sessionChangesLoading || state.sessionChangesError) {
      return;
    }
    if (state.activeChangeset?.session_id === activeSession.session_id) {
      return;
    }
    const timerId = window.setTimeout(() => {
      void refreshSessionChanges(activeSession.session_id);
    }, 120);
    return () => window.clearTimeout(timerId);
  }, [
    activeSession,
    auxiliaryTab,
    auxiliaryVisible,
    refreshSessionChanges,
    state.activeChangeset,
    state.contentView,
    state.sessionChangesError,
    state.sessionChangesLoading,
  ]);
  const handleToggleAuxiliaryPanel = () => {
    const nextVisible = !auxiliaryVisible;
    setAuxiliaryVisible(nextVisible);
    persistLayoutSettings({ auxiliary_visible: nextVisible });
    setStatus(nextVisible ? "已显示右侧侧边栏" : "已隐藏右侧侧边栏");
  };
  const startLayoutResize = (
    target: LayoutResizeTarget,
    event: ReactPointerEvent<HTMLButtonElement>,
  ) => {
    event.preventDefault();
    cleanupLayoutResizeRef.current?.();

    const startX = event.clientX;
    const startRatios = mainAreaRatios;
    const effectiveStartRatios = previewMaximized
      ? {
          ...startRatios,
          workspace_preview:
            startRatios.agent_sessions +
            startRatios.chat +
            startRatios.workspace_preview,
        }
      : startRatios;
    const [left, leftSelector, right, rightSelector]: [
      MainAreaKey,
      string,
      MainAreaKey,
      string,
    ] = target === "agent-sessions-right"
      ? ["agent_sessions", ".agent-sessions-panel", "chat", ".sessions-part-card"]
      : target === "preview-left"
        ? ["chat", ".sessions-part-card", "workspace_preview", ".workspace-preview-panel"]
        : previewVisible
          ? ["workspace_preview", ".workspace-preview-panel", "auxiliary", ".auxiliary-panel"]
          : ["chat", ".sessions-part-card", "auxiliary", ".auxiliary-panel"];
    const leftArea = document.querySelector<HTMLElement>(leftSelector);
    const rightArea = document.querySelector<HTMLElement>(rightSelector);
    if (!leftArea || !rightArea) {
      throw new Error(
        `主页布局区域不存在: left=${leftSelector}, right=${rightSelector}`,
      );
    }
    const leftWidth = leftArea.getBoundingClientRect().width;
    const rightWidth = rightArea.getBoundingClientRect().width;
    let latestRatios: WebUiMainAreaRatios = startRatios;
    let moved = false;

    const handlePointerMove = (moveEvent: PointerEvent) => {
      const deltaX = moveEvent.clientX - startX;
      if (deltaX === 0) {
        return;
      }
      moved = true;
      const resizedRatios = resizeAdjacentMainAreas({
        ratios: effectiveStartRatios,
        left,
        right,
        leftWidth,
        rightWidth,
        deltaX,
      });
      if (previewMaximized && left === "workspace_preview") {
        const scale =
          resizedRatios.workspace_preview /
          effectiveStartRatios.workspace_preview;
        latestRatios = {
          agent_sessions: startRatios.agent_sessions * scale,
          chat: startRatios.chat * scale,
          workspace_preview: startRatios.workspace_preview * scale,
          auxiliary: resizedRatios.auxiliary,
        };
      } else {
        latestRatios = resizedRatios;
      }
      setMainAreaRatios(latestRatios);
    };

    const finishResize = () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", finishResize);
      window.removeEventListener("pointercancel", finishResize);
      document.body.classList.remove(LAYOUT_RESIZING_CLASS);
      cleanupLayoutResizeRef.current = null;
      if (moved) {
        persistLayoutSettings({ main_area_ratios: latestRatios });
      }
    };

    document.body.classList.add(LAYOUT_RESIZING_CLASS);
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", finishResize);
    window.addEventListener("pointercancel", finishResize);
    cleanupLayoutResizeRef.current = finishResize;
  };
  const resetMainAreaRatios = () => {
    const ratios = { ...DEFAULT_MAIN_AREA_RATIOS };
    setMainAreaRatios(ratios);
    persistLayoutSettings({ main_area_ratios: ratios });
  };
  const handleCreateSession = (workspaceId?: string | null) => {
    setNameDialog(null);
    setNameDialogError(null);
    startNewSessionDraft(workspaceId);
  };
  const handleSelectAgentSession = (workspaceId: string, sessionId: string) => {
    selectWorkspaceSession(workspaceId, sessionId);
  };
  const handleRemoveWorkspace = (workspaceId: string, workspaceName: string) => {
    const confirmed = window.confirm(
      `删除工作区「${workspaceName || workspaceId}」？会话文件不会被删除，只会从 Web Gateway 列表移除。`,
    );
    if (!confirmed) {
      return;
    }
    void removeGatewayWorkspace(workspaceId).catch(() => {
      // 失败状态由 removeGatewayWorkspace 写入全局状态。
    });
  };
  const handleRenameSession = (sessionId: string, currentTitle: string) => {
    setNameDialog({
      sessionId,
      initialTitle: currentTitle || "新会话",
    });
    setNameDialogError(null);
  };
  const handleDeleteSession = (sessionId: string, title: string) => {
    const label = title || sessionId;
    const confirmed = window.confirm(
      `删除会话「${label}」？相关后台任务会一并清理。`,
    );
    if (!confirmed) {
      return;
    }
    void deleteSession(sessionId).catch(() => {
      // 删除失败时由 deleteSession 写入全局状态，这里只避免未处理 Promise。
    });
  };
  const closeNameDialog = () => {
    if (nameDialogSubmitting) {
      return;
    }
    setNameDialog(null);
    setNameDialogError(null);
  };
  const submitNameDialog = (title: string) => {
    if (!nameDialog) {
      return;
    }

    setNameDialogSubmitting(true);
    setNameDialogError(null);
    const action = renameSession(nameDialog.sessionId, title);

    void action
      .then(() => {
        setNameDialog(null);
      })
      .catch((error: unknown) => {
        setNameDialogError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        setNameDialogSubmitting(false);
      });
  };
  const findLatestResponseInConversation = (marker: Element | null) => {
    let target: Element | null = marker;
    let cursor = marker?.nextElementSibling ?? null;
    while (cursor && !cursor.classList.contains("conversation-marker")) {
      if (cursor.classList.contains("event-card-response")) {
        target = cursor;
      }
      cursor = cursor.nextElementSibling;
    }
    return target;
  };
  const showConversation = (jobId?: string) => {
    switchContentView("default");
    if (!jobId) {
      window.setTimeout(() => {
        const markers = document.querySelectorAll(".conversation-marker");
        const marker = markers.length > 0 ? markers[markers.length - 1] : null;
        const target = findLatestResponseInConversation(marker);
        if (target instanceof HTMLElement) {
          target.scrollIntoView({ behavior: "smooth", block: "start" });
          return;
        }
        const stream = document.querySelector<HTMLElement>(".chat-stream");
        stream?.scrollTo({ top: stream.scrollHeight, behavior: "smooth" });
      }, 80);
      return;
    }

    window.setTimeout(() => {
      const escapedJobId = jobId.replace(/["\\]/g, "\\$&");
      const marker = document.querySelector(`[data-job-id="${escapedJobId}"]`);
      const target = findLatestResponseInConversation(marker);
      const targetElement = target instanceof HTMLElement ? target : null;
      targetElement?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 80);
  };

  const renderContentView = () => {
    if (state.error) {
      return (
        <div className="empty-state error-state">
          <div className="error-title">前端初始化失败</div>
          <div className="error-message">{state.error}</div>
        </div>
      );
    }

    if (state.isBootstrapping) {
      return <BootstrapState />;
    }

    if (state.contentView === "agent") {
      return (
        <AgentStatePanel
          jsonl={state.agentStateJsonl}
          messageCount={state.agentStateMessageCount}
          loadedAt={state.agentStateLoadedAt}
          loading={state.agentStateLoading}
          error={state.agentStateError}
        />
      );
    }

    if (state.contentView === "events") {
      return (
        <EventQueuePanel
          items={receivedEvents}
          limit={FRONTEND_EVENT_QUEUE_LIMIT}
          sessionId={activeSession?.session_id ?? ""}
        />
      );
    }

    if (state.contentView === "requests") {
      return (
        <RequestLogPanel
          logs={state.llmRequestLogs}
          loading={state.llmRequestLogsLoading}
          error={state.llmRequestLogsError}
          loadedAt={state.llmRequestLogsLoadedAt}
          sessionId={activeSession?.session_id ?? ""}
        />
      );
    }

    if (state.contentView === "resources") {
      return (
        <ResourcePanel
          resources={state.sessionResources}
          loading={state.sessionResourcesLoading}
          error={state.sessionResourcesError}
          loadedAt={state.sessionResourcesLoadedAt}
          sessionId={activeSession?.session_id ?? ""}
          onRefresh={() => {
            if (activeSession) {
              void refreshSessionResources(activeSession.session_id);
            }
          }}
          onControl={controlSessionResource}
          onOpenTerminalPreview={workspacePreview.openTerminalPreview}
          onOpenBrowserPreview={workspacePreview.openBrowserPreview}
          onShowConversation={showConversation}
        />
      );
    }

    const activeSessionChangeHint =
      defaultViewChangesHint &&
      defaultViewChangesHint.sessionId === activeSession?.session_id
        ? defaultViewChangesHint.summary
        : state.contentView === "changes" && state.activeChangeset
          ? state.activeChangeset.summary
        : null;

    return (
      <ChatPanel
        conversations={conversations}
        expandDetails={state.expandDetails}
        hasActiveSession={Boolean(activeSession)}
        sessionChangeSummary={activeSessionChangeHint}
        sessionChangesLoading={defaultViewChangesLoading}
        onOpenChanges={() => {
          void switchContentView("changes");
        }}
      />
    );
  };

  return (
    <WorkspaceFileReferenceProvider
      key={activeSessionCacheKey ?? "no-session"}
      apiPort={resolvedApiPort}
      workspaceId={activeSessionWorkspaceId}
      workspaceRoot={state.workspaceRoot ?? ""}
      onOpen={workspacePreview.openWorkspaceFileReference}
    >
      <div
      className={`app-shell agent-sessions-workbench shell-gradient-background ${agentSessionsVisible ? "agent-sessions-open" : "agent-sessions-closed"}`}
      data-agent-sessions-open={String(agentSessionsVisible)}
    >
      <Toolbar
        sessionTitle={state.currentSession?.title ?? null}
        onCreateSession={handleCreateSession}
        auxiliaryVisible={auxiliaryVisible}
        onToggleAuxiliaryPanel={handleToggleAuxiliaryPanel}
      />
      <main className="content sessions-workbench-grid">
        <div
          className={`content-layout${auxiliaryVisible ? "" : " auxiliary-collapsed"}${previewVisible ? "" : " preview-collapsed"}${previewMaximized ? " preview-maximized" : ""}`}
        >
          <AgentSessionsPanel
            sessions={sortedSessions}
            currentSessionId={activeSession?.session_id ?? ""}
            onSelectSession={selectSession}
            onRenameSession={handleRenameSession}
            onDeleteSession={handleDeleteSession}
            onSetSessionParent={setSessionParent}
            onForkSessionContext={forkSessionContext}
            onStatusChange={setStatus}
            isOpen={agentSessionsVisible}
            workspaceName={state.workspaceName ?? ""}
            gatewayWorkspaces={state.gatewayWorkspaces}
            activeGatewayWorkspaceId={state.activeGatewayWorkspaceId}
            sessionsByWorkspace={state.sessionsByWorkspace}
            workspaceSwitching={state.workspaceSwitching}
            removingGatewayWorkspaceIds={state.removingGatewayWorkspaceIds}
            onActivateWorkspace={activateGatewayWorkspace}
            onRemoveWorkspace={handleRemoveWorkspace}
            onReorderWorkspaces={reorderGatewayWorkspaces}
            onSelectWorkspaceSession={handleSelectAgentSession}
            activeSession={activeSession}
            sessionAttachmentSummaries={state.sessionAttachmentSummaries}
            onCreateSession={handleCreateSession}
            flexRatio={mainAreaRatios.agent_sessions}
            customizationsCollapsed={customizationsCollapsed}
            customizationsHeight={customizationsHeight}
            onCustomizationsCollapsedChange={(collapsed) => {
              setCustomizationsCollapsed(collapsed);
              persistLayoutSettings({ customizations_collapsed: collapsed });
            }}
            onCustomizationsHeightChange={(height, commit) => {
              setCustomizationsHeight(height);
              if (commit) {
                persistLayoutSettings({ customizations_height: height });
              }
            }}
          />
          {agentSessionsVisible ? (
            <button
              type="button"
              className="layout-sash layout-sash-agent-sessions-right"
              title="拖拽调整会话侧栏宽度，双击还原"
              aria-label="调整会话侧栏宽度"
              onPointerDown={(event) => startLayoutResize("agent-sessions-right", event)}
              onDoubleClick={resetMainAreaRatios}
            />
          ) : null}
          <section
            className="chat-panel sessions-part-card"
            style={{ flexBasis: 0, flexGrow: mainAreaRatios.chat }}
          >
            <div className="session-view-surface">
              <div className="session-view-content">{renderContentView()}</div>
              <Composer />
            </div>
          </section>
          {previewVisible ? (
            <>
              <button
                type="button"
                className="layout-sash layout-sash-preview-left"
                title="拖拽调整文件预览区宽度，双击还原"
                aria-label="调整文件预览区宽度"
                onPointerDown={(event) => startLayoutResize("preview-left", event)}
                onDoubleClick={resetMainAreaRatios}
              />
              <WorkspaceFilePreviewArea
                flexRatio={
                  previewMaximized
                    ? mainAreaRatios.agent_sessions +
                      mainAreaRatios.chat +
                      mainAreaRatios.workspace_preview
                    : mainAreaRatios.workspace_preview
                }
                maximized={previewMaximized}
                tabs={previewTabs}
                activePath={activePreviewPath}
                loadingPath={previewLoadingPath}
                error={previewError}
                onSelectTab={(path) => {
                  workspacePreview.selectWorkspacePreviewTab(path);
                  workspacePreview.setError(null);
                }}
                onCloseTab={workspacePreview.closeWorkspaceFilePreview}
                onToggleMaximized={() => {
                  workspacePreview.setMaximized((maximized) => !maximized);
                }}
                onClosePanel={() => {
                  workspacePreview.setMaximized(false);
                  workspacePreview.setVisible(false);
                }}
              />
            </>
          ) : null}
          {auxiliaryVisible ? (
            <>
              <button
                type="button"
                className="layout-sash layout-sash-auxiliary-left"
                title="拖拽调整右侧栏宽度，双击还原"
                aria-label="调整右侧栏宽度"
                onPointerDown={(event) => startLayoutResize("auxiliary-left", event)}
                onDoubleClick={resetMainAreaRatios}
              />
              <WorkspaceAuxiliaryPanel
                flexRatio={mainAreaRatios.auxiliary}
                tab={auxiliaryTab}
                apiPort={resolvedApiPort}
                workspaceId={activeSessionWorkspaceId}
                workspaceName={state.workspaceName ?? ""}
                workspaceRoot={state.workspaceRoot ?? ""}
                activeFilePath={activePreviewPath}
                sessionChangesets={state.sessionChangesets}
                selectedChangesetId={state.selectedChangesetId}
                activeChangeset={state.activeChangeset}
                sessionChangesLoading={state.sessionChangesLoading}
                sessionChangesError={state.sessionChangesError}
                sessionChangesLoadedAt={state.sessionChangesLoadedAt}
                searchOpen={fileTreeSearchOpen}
                collapseVersion={fileTreeCollapseVersion}
                onTabChange={setAuxiliaryTab}
                onToggleSearch={() => {
                  setAuxiliaryTab("files");
                  setFileTreeSearchOpen((open) => !open);
                }}
                onCollapseAll={() => {
                  setAuxiliaryTab("files");
                  setFileTreeCollapseVersion((version) => version + 1);
                }}
                onSelectSessionChangeset={(changesetId) => {
                  if (activeSession) {
                    void refreshSessionChanges(activeSession.session_id, changesetId);
                  }
                }}
                onRefreshSessionChanges={() => {
                  if (activeSession) {
                    void refreshSessionChanges(
                      activeSession.session_id,
                      state.selectedChangesetId,
                    );
                  }
                }}
                onOpenSessionChangeFile={openSessionChangeInPreview}
                onReviewSessionChangeFile={reviewSessionChangeFile}
                onOpenFile={workspacePreview.openWorkspaceFilePreview}
                onStatusChange={setStatus}
              />
            </>
          ) : null}
        </div>
      </main>
      <SessionNameDialog
        open={nameDialog !== null}
        title="重命名会话"
        label="会话名称"
        initialValue={nameDialog?.initialTitle ?? "新会话"}
        confirmText="保存名称"
        submitting={nameDialogSubmitting}
        error={nameDialogError}
        onCancel={closeNameDialog}
        onSubmit={submitNameDialog}
      />
      </div>
    </WorkspaceFileReferenceProvider>
  );
}
