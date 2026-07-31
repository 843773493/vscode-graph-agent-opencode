import AgentStatePanel from "./components/AgentStatePanel";
import BootstrapState from "./components/BootstrapState";
import ChatPanel from "./components/ChatPanel";
import Composer from "./components/composer/Composer";
import EventQueuePanel from "./components/EventQueuePanel";
import AgentSessionsPanel from "./components/AgentSessionsPanel";
import RequestLogPanel from "./components/RequestLogPanel";
import ResourcePanel from "./components/ResourcePanel";
import SessionNameDialog from "./components/SessionNameDialog";
import { useWarmConfirm } from "./components/WarmConfirmProvider";
import Toolbar, { type WorkbenchView } from "./components/Toolbar";
import GatewayControlCenter from "./components/workspace/GatewayControlCenter";
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
import {
  DEFAULT_BACKEND_PORT,
  DEFAULT_SESSION_TITLE,
  createSessionCatalogFolder,
  getSessionChangesets,
} from "./api";
import {
  FRONTEND_EVENT_QUEUE_LIMIT,
  getConversationsForSession,
  useAppState,
} from "./hooks";
import { useWorkspacePreviewTabs } from "./hooks/useWorkspacePreviewTabs";
import { buildSessionCatalogSyncKeys } from "./hooks/sessionResourceExplorer/resourceTreeSync";
import { createSessionConnection } from "./gatewayApi";
import {
  DEFAULT_MAIN_AREA_RATIOS,
  LAYOUT_RESIZING_CLASS,
  defaultAuxiliaryVisible,
  resizeAdjacentMainAreas,
  resolveMainAreaRatios,
  type MainAreaKey,
  type LayoutResizeTarget,
} from "./layout/workbenchLayout";
import { sessionScopeKey } from "./state/session/sessionScope";
import { resolveAgentSessionsPreferences } from "./state/uiSettings/preferences";
import type {
  SessionChangesSummary,
  SessionFileChange,
  WebUiMainAreaRatios,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "./types/backend";

type SessionNameDialogState = {
  sessionId: string;
  workspaceId: string;
  initialTitle: string;
};

export default function AppShell() {
  const confirm = useWarmConfirm();
  const {
    state,
    createSession,
    selectSession,
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
    refreshGatewayState,
    reconnectGatewayWorkspace,
    startManagedGatewayWorkspaceBackend,
    stopManagedGatewayWorkspaceBackend,
    addManagedGatewayWorkspace,
    addSshGatewayWorkspace,
    removeGatewayWorkspace,
    renameGatewayWorkspace,
    setGatewayWorkspaceParent,
    refreshGatewayWorkspaceSessions,
    copySessionInformation,
    copyWorkspaceInformation,
    updateUiSettings,
    setStatus,
    replayTurn,
    updatePendingRequest,
    removePendingRequest,
    clearPendingRequests,
    reorderPendingRequests,
    sendPendingRequestImmediately,
    refreshGoal,
    updateGoal,
    clearGoal,
    loadOlderMessages,
    loadTurnDetails,
    refreshTurnHistory,
    loadOlderTraceHistory,
    refreshTraceHistory,
  } = useAppState();
  const [nameDialog, setNameDialog] = useState<SessionNameDialogState | null>(null);
  const [sessionCatalogRefreshVersions, setSessionCatalogRefreshVersions] =
    useState<ReadonlyMap<string, number>>(new Map());
  const [workbenchView, setWorkbenchView] = useState<WorkbenchView>(
    () => state.uiSettings.layout.workbench_view ?? "sessions",
  );
  const [nameDialogSubmitting, setNameDialogSubmitting] = useState(false);
  const [nameDialogError, setNameDialogError] = useState<string | null>(null);
  const [auxiliaryTab, setAuxiliaryTab] = useState<WorkspaceAuxiliaryTab>(
    () => state.uiSettings.layout.auxiliary_tab ?? "changes",
  );
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
  const activeTurnTimeline = activeSessionCacheKey
    ? state.turnTimelinesBySession.get(activeSessionCacheKey) ?? null
    : null;
  const agentSessionsPreferences = useMemo(
    () => resolveAgentSessionsPreferences(state.uiSettings),
    [state.uiSettings],
  );
  const sessionCatalogSyncKeys = useMemo(
    () => buildSessionCatalogSyncKeys(state.sessionsByWorkspace),
    [state.sessionsByWorkspace],
  );
  const invalidateSessionCatalog = useCallback((workspaceId: string) => {
    setSessionCatalogRefreshVersions((previous) => {
      const next = new Map(previous);
      next.set(workspaceId, (previous.get(workspaceId) ?? 0) + 1);
      return next;
    });
  }, []);
  const expandedFileTreePaths = useMemo(() => {
    if (!activeSessionWorkspaceId) {
      return [""];
    }
    return state.uiSettings.workspace_file_tree.expanded_paths_by_workspace[
      activeSessionWorkspaceId
    ] ?? [""];
  }, [activeSessionWorkspaceId, state.uiSettings.workspace_file_tree]);

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
    if (layout.workbench_view) {
      setWorkbenchView(layout.workbench_view);
    }
    if (typeof layout.auxiliary_visible === "boolean") {
      setAuxiliaryVisible(layout.auxiliary_visible);
    }
    if (layout.auxiliary_tab) {
      setAuxiliaryTab(layout.auxiliary_tab);
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
      state.pendingConversations,
      state.turnTimelinesBySession,
    ],
  );
  const activeTraceHistory = activeSession
    ? state.sessionTraceHistoryBySession.get(
        activeSessionCacheKey ?? activeSession.session_id,
      ) ?? null
    : null;
  const receivedEvents = useMemo(() => {
    if (!activeSession) return [];
    const scopeKey = activeSessionCacheKey ?? activeSession.session_id;
    const historical = (activeTraceHistory?.items ?? []).map((event) => ({
      id: `initial_load:${event.event_id}`,
      kind: "trace" as const,
      sessionId: activeSession.session_id,
      receivedAt: event.timestamp,
      source: "initial_load" as const,
      event,
    }));
    return [
      ...historical,
      ...(state.eventQueuesBySession.get(scopeKey) ?? []),
    ];
  }, [
    activeSession,
    activeSessionCacheKey,
    activeTraceHistory?.items,
    state.eventQueuesBySession,
  ]);
  const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
  const sortedSessions = useMemo(
    () => [...state.sessions].sort(
      (a, b) =>
        new Date(b.updated_at || b.created_at || "").getTime() -
        new Date(a.updated_at || a.created_at || "").getTime(),
    ),
    [state.sessions],
  );
  const persistUiSettings = useCallback(
    (
      input: WebUiSettingsUpdate
        | ((current: WebUiSettings) => WebUiSettingsUpdate),
    ) => {
      void updateUiSettings(input).catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        setStatus(`保存页面设置失败: ${message}`);
      });
    },
    [setStatus, updateUiSettings],
  );
  const persistLayoutSettings = useCallback(
    (layout: WebUiSettingsUpdate["layout"]) => {
      persistUiSettings({ layout });
    },
    [persistUiSettings],
  );
  const agentSessionsVisible = state.agentSessionsPanelOpen;
  const handleWorkbenchViewChange = useCallback(
    (view: WorkbenchView) => {
      setWorkbenchView(view);
      persistLayoutSettings({ workbench_view: view });
    },
    [persistLayoutSettings],
  );
  const workspacePreview = useWorkspacePreviewTabs({
    apiPort: resolvedApiPort,
    workspaceId: activeSessionWorkspaceId,
    workspaceRoot: state.workspaceRoot ?? "",
    settingsLoaded: state.uiSettingsLoaded,
    restoredLayout: state.uiSettings.layout,
    onPersistLayout: persistLayoutSettings,
    onStatusChange: setStatus,
  });
  const initializationFailed = Boolean(state.error && !state.isBootstrapping);
  const previewVisible = !initializationFailed && workspacePreview.visible;
  const previewMaximized = previewVisible && workspacePreview.maximized;
  const previewTabs = workspacePreview.tabs;
  const activePreviewPath = workspacePreview.activePath;
  const previewLoadingPath = workspacePreview.loadingPath;
  const previewError = workspacePreview.error;
  const resourcePanelActive = auxiliaryVisible && auxiliaryTab === "resources";

  useEffect(() => {
    if (state.contentView !== "resources") {
      return;
    }
    setAuxiliaryVisible(true);
    setAuxiliaryTab("resources");
    persistLayoutSettings({ auxiliary_visible: true, auxiliary_tab: "resources" });
    switchContentView("default");
  }, [persistLayoutSettings, state.contentView, switchContentView]);

  useEffect(() => {
    if (!resourcePanelActive || !activeSession) {
      return;
    }

    let disposed = false;
    let pollInFlight = false;
    const poll = async (silent: boolean) => {
      if (disposed || pollInFlight || document.visibilityState !== "visible") {
        return;
      }
      pollInFlight = true;
      try {
        await refreshSessionResources(activeSession.session_id, { silent });
      } finally {
        pollInFlight = false;
      }
    };

    const initialTimerId = window.setTimeout(() => void poll(false), 120);
    const timerId = window.setInterval(() => void poll(true), 5000);
    return () => {
      disposed = true;
      window.clearTimeout(initialTimerId);
      window.clearInterval(timerId);
    };
  }, [activeSession, refreshSessionResources, resourcePanelActive]);

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

    // 刷新恢复页面设置时，显式隐藏右侧栏的选择必须优先于上次内容视图。
    // 用户重新打开右侧栏后，updateUiSettings 返回的新设置会移除此条件。
    if (state.uiSettings.layout.auxiliary_visible !== false) {
      setAuxiliaryVisible(true);
    }
  }, [state.contentView, state.uiSettings.layout.auxiliary_visible]);

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
  const handleAuxiliaryTabChange = (tab: WorkspaceAuxiliaryTab) => {
    setAuxiliaryTab(tab);
    persistLayoutSettings({ auxiliary_tab: tab });
  };
  const handleOpenChangesView = () => {
    setAuxiliaryVisible(true);
    setAuxiliaryTab("changes");
    persistLayoutSettings({ auxiliary_visible: true, auxiliary_tab: "changes" });
    void switchContentView("changes");
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
  const handleCreateSessionInFolder = async (
    workspaceId: string,
    folderId: string,
  ) => {
    if (workspaceId !== state.activeGatewayWorkspaceId) {
      await activateGatewayWorkspace(workspaceId);
    }
    await createSession(DEFAULT_SESSION_TITLE, workspaceId, folderId);
    invalidateSessionCatalog(workspaceId);
  };
  const handleCreateSessionFolder = async (
    workspaceId: string,
    parentNodeId: string | null,
    name: string,
  ) => {
    await createSessionCatalogFolder(
      resolvedApiPort,
      workspaceId,
      name,
      parentNodeId,
    );
    invalidateSessionCatalog(workspaceId);
  };
  const handleSessionFolderDeleted = async (
    workspaceId: string,
    deletedCurrentSession: boolean,
  ) => {
    if (workspaceId !== state.activeGatewayWorkspaceId) {
      return;
    }
    await activateGatewayWorkspace(
      workspaceId,
      deletedCurrentSession ? null : activeSession?.session_id ?? null,
    );
  };
  const handleSelectAgentSession = async (
    workspaceId: string,
    sessionId: string,
  ) => {
    if (workspaceId !== state.activeGatewayWorkspaceId) {
      await activateGatewayWorkspace(workspaceId, sessionId);
      return;
    }
    selectSession(sessionId);
  };
  const handleRemoveWorkspace = (workspaceId: string, workspaceName: string) => {
    const label = workspaceName || workspaceId;
    void confirm({
      title: "删除工作区",
      message: `从 Web Gateway 列表移除工作区“${label}”。会话文件不会被删除。`,
      confirmText: "删除",
      danger: true,
    }).then(async (confirmed) => {
      if (confirmed) {
        await removeGatewayWorkspace(workspaceId);
      }
    }).catch((error: unknown) => {
      setStatus(`删除工作区失败: ${error instanceof Error ? error.message : String(error)}`);
    });
  };
  const handleUseGatewayWorkspace = async (workspaceId: string) => {
    if (workspaceId !== state.activeGatewayWorkspaceId) {
      await activateGatewayWorkspace(workspaceId);
    }
    handleWorkbenchViewChange("sessions");
  };
  const handleRenameSession = (
    sessionId: string,
    currentTitle: string,
    workspaceId: string,
  ) => {
    setNameDialog({
      sessionId,
      workspaceId,
      initialTitle: currentTitle || "新会话",
    });
    setNameDialogError(null);
  };
  const handleDeleteSession = (
    sessionId: string,
    title: string,
    workspaceId: string,
  ) => {
    const label = title || sessionId;
    void confirm({
      title: "永久删除会话",
      message: `永久删除会话“${label}”。如果它包含子会话，将级联删除整棵子会话树及其消息、检查点、日志、附件和运行资源。此操作无法撤销。`,
      confirmText: "删除",
      danger: true,
    }).then(async (confirmed) => {
      if (!confirmed) {
        return;
      }
      await deleteSession(sessionId, workspaceId);
      invalidateSessionCatalog(workspaceId);
    }).catch((error: unknown) => {
      setStatus(`删除会话失败: ${error instanceof Error ? error.message : String(error)}`);
    });
  };
  const handleSetSessionParent = async (
    workspaceId: string,
    sessionId: string,
    parentSessionId: string | null,
  ) => {
    await setSessionParent(workspaceId, sessionId, parentSessionId);
    invalidateSessionCatalog(workspaceId);
  };
  const handleForkSessionContext = async (
    workspaceId: string,
    sourceSessionId: string,
  ) => {
    await forkSessionContext(workspaceId, sourceSessionId);
    invalidateSessionCatalog(workspaceId);
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
    const action = renameSession(
      nameDialog.sessionId,
      title,
      nameDialog.workspaceId,
    );

    void action
      .then(() => {
        invalidateSessionCatalog(nameDialog.workspaceId);
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

    const contentView = state.contentView;
    const conversationVisible = ![
      "agent",
      "events",
      "requests",
    ].includes(contentView);
    const activeSessionChangeHint =
      defaultViewChangesHint &&
      defaultViewChangesHint.sessionId === activeSession?.session_id
        ? defaultViewChangesHint.summary
        : contentView === "changes" && state.activeChangeset
          ? state.activeChangeset.summary
          : null;

    return (
      <>
        <div
          className={`content-view-slot${
            contentView === "agent" ? "" : " preserve-mounted-hidden"
          }`}
          hidden={contentView !== "agent"}
        >
        <AgentStatePanel
          jsonl={state.agentStateJsonl}
          messageCount={state.agentStateMessageCount}
          loadedAt={state.agentStateLoadedAt}
          loading={state.agentStateLoading}
          error={state.agentStateError}
        />
        </div>
        <div
          className={`content-view-slot${
            contentView === "events" ? "" : " preserve-mounted-hidden"
          }`}
          hidden={contentView !== "events"}
        >
        <EventQueuePanel
          items={receivedEvents}
          limit={FRONTEND_EVENT_QUEUE_LIMIT}
          sessionId={activeSession?.session_id ?? ""}
          active={contentView === "events"}
          historyLoading={activeTraceHistory?.loading ?? false}
          historyLoadingOlder={activeTraceHistory?.loadingOlder ?? false}
          historyHasMore={activeTraceHistory?.hasMore ?? false}
          historyError={activeTraceHistory?.error ?? null}
          onLoadOlderHistory={loadOlderTraceHistory}
          onRetryHistory={() => void refreshTraceHistory()}
        />
        </div>
        <div
          className={`content-view-slot${
            contentView === "requests" ? "" : " preserve-mounted-hidden"
          }`}
          hidden={contentView !== "requests"}
        >
        <RequestLogPanel
          logs={state.llmRequestLogs}
          loading={state.llmRequestLogsLoading}
          error={state.llmRequestLogsError}
          loadedAt={state.llmRequestLogsLoadedAt}
          sessionId={activeSession?.session_id ?? ""}
          active={contentView === "requests"}
        />
        </div>
        <div
          className={`content-view-slot${
            conversationVisible ? "" : " preserve-mounted-hidden"
          }`}
          hidden={!conversationVisible}
        >
          <ChatPanel
            apiPort={resolvedApiPort}
            workspaceId={activeSessionWorkspaceId}
            conversations={conversations}
            expandDetails={state.expandDetails}
            hasActiveSession={Boolean(activeSession)}
            hasOlderMessages={activeTurnTimeline?.hasMore ?? false}
            loadingOlderMessages={activeTurnTimeline?.loadingOlder ?? false}
            historyLoading={Boolean(activeSession) && (
              !activeTurnTimeline || activeTurnTimeline.phase === "bootstrapping"
            )}
            projectionState={activeTurnTimeline?.projectionState ?? "ready"}
            timelineGeneration={activeTurnTimeline?.generation ?? 0}
            projectionEpoch={activeTurnTimeline?.projectionEpoch ?? null}
            historyError={activeTurnTimeline?.error ?? null}
            onLoadOlderMessages={loadOlderMessages}
            loadingDetailTurnIds={activeTurnTimeline?.loadingDetailIds ?? []}
            onLoadTurnDetails={loadTurnDetails}
            onRetryHistory={refreshTurnHistory}
            sessionChangeSummary={activeSessionChangeHint}
            sessionChangesLoading={defaultViewChangesLoading}
            onOpenChanges={handleOpenChangesView}
            onReplayTurn={replayTurn}
            onUpdatePending={updatePendingRequest}
            onRemovePending={removePendingRequest}
            onClearPending={clearPendingRequests}
            onReorderPending={reorderPendingRequests}
            onSendPendingImmediately={sendPendingRequestImmediately}
          />
        </div>
      </>
    );
  };

  return (
    <WorkspaceFileReferenceProvider
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
        sessionTitle={
          workbenchView === "gateway"
            ? "Gateway 控制台"
            : state.currentSession?.title ?? null
        }
        onCreateSession={() => {
          if (workbenchView === "gateway") {
            handleWorkbenchViewChange("sessions");
          }
          handleCreateSession();
        }}
        auxiliaryVisible={auxiliaryVisible}
        onToggleAuxiliaryPanel={handleToggleAuxiliaryPanel}
        workbenchView={workbenchView}
        onWorkbenchViewChange={handleWorkbenchViewChange}
        showAuxiliaryToggle={workbenchView === "sessions"}
      />
      <div
        className={`gateway-view-slot${
          workbenchView === "gateway" ? "" : " preserve-mounted-hidden"
        }`}
        hidden={workbenchView !== "gateway"}
      >
        <GatewayControlCenter
          apiPort={resolvedApiPort}
          workspaces={state.gatewayWorkspaces}
          gatewayError={state.gatewayError}
          onAddSsh={addSshGatewayWorkspace}
          onRefresh={refreshGatewayState}
          onReconnect={reconnectGatewayWorkspace}
        />
      </div>
      <main
        className={`content sessions-workbench-grid${
          workbenchView === "sessions" ? "" : " preserve-mounted-hidden"
        }`}
        hidden={workbenchView !== "sessions"}
      >
        <div
          className={`content-layout${auxiliaryVisible ? "" : " auxiliary-collapsed"}${previewVisible ? "" : " preview-collapsed"}${previewMaximized ? " preview-maximized" : ""}`}
        >
          <AgentSessionsPanel
            apiPort={resolvedApiPort}
            sessions={sortedSessions}
            currentSessionId={activeSession?.session_id ?? ""}
            onSelectSession={selectSession}
            onRenameSession={handleRenameSession}
            onDeleteSession={handleDeleteSession}
            onSetSessionParent={handleSetSessionParent}
            onForkSessionContext={handleForkSessionContext}
            onStatusChange={setStatus}
            isOpen={agentSessionsVisible}
            workspaceName={state.workspaceName ?? ""}
            gatewayWorkspaces={state.gatewayWorkspaces}
            activeGatewayWorkspaceId={state.activeGatewayWorkspaceId}
            workspaceSwitching={state.workspaceSwitching}
            onActivateWorkspace={activateGatewayWorkspace}
            onSetWorkspaceParent={setGatewayWorkspaceParent}
            onRefreshWorkspaceSessions={refreshGatewayWorkspaceSessions}
            onRemoveWorkspace={handleRemoveWorkspace}
            onAddWorkspace={addManagedGatewayWorkspace}
            onOpenGatewayControl={() => handleWorkbenchViewChange("gateway")}
            onStartWorkspace={startManagedGatewayWorkspaceBackend}
            onStopWorkspace={stopManagedGatewayWorkspaceBackend}
            onRenameWorkspace={renameGatewayWorkspace}
            onCopySessionInformation={copySessionInformation}
            onCopyWorkspaceInformation={copyWorkspaceInformation}
            onSelectWorkspaceSession={handleSelectAgentSession}
            activeSession={activeSession}
            sessionAttachmentSummaries={state.sessionAttachmentSummaries}
            activeJobIdsBySession={state.activeJobIdsBySession}
            unreadSessionKeys={state.unreadSessionKeys}
            onCreateSession={handleCreateSession}
            onCreateSessionInFolder={handleCreateSessionInFolder}
            onCreateSessionFolder={handleCreateSessionFolder}
            onSessionFolderDeleted={handleSessionFolderDeleted}
            catalogSyncKeys={sessionCatalogSyncKeys}
            catalogRefreshVersions={sessionCatalogRefreshVersions}
            flexRatio={mainAreaRatios.agent_sessions}
            preferences={agentSessionsPreferences}
            onPreferencesChange={(updater) => {
              persistUiSettings((current) => ({
                session_sidebar: updater(current.session_sidebar),
              }));
            }}
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
            <button
              type="button"
              className="layout-sash layout-sash-preview-left"
              title="拖拽调整文件预览区宽度，双击还原"
              aria-label="调整文件预览区宽度"
              onPointerDown={(event) => startLayoutResize("preview-left", event)}
              onDoubleClick={resetMainAreaRatios}
            />
          ) : null}
          <WorkspaceFilePreviewArea
            visible={previewVisible}
            apiPort={resolvedApiPort}
            workspaceId={activeSessionWorkspaceId}
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
            editingPath={workspacePreview.editingPath}
            draftContent={workspacePreview.draftContent}
            savingPath={workspacePreview.savingPath}
            hasUnsavedEdit={workspacePreview.hasUnsavedEdit}
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
            onBeginEdit={workspacePreview.beginWorkspaceFileEdit}
            onDraftChange={workspacePreview.setDraftContent}
            onCancelEdit={workspacePreview.cancelWorkspaceFileEdit}
            onSaveEdit={workspacePreview.saveWorkspaceFileEdit}
            onOpenWorkspacePath={workspacePreview.openWorkspaceFilePath}
          />
          {auxiliaryVisible ? (
            <button
              type="button"
              className="layout-sash layout-sash-auxiliary-left"
              title="拖拽调整右侧栏宽度，双击还原"
              aria-label="调整右侧栏宽度"
              onPointerDown={(event) => startLayoutResize("auxiliary-left", event)}
              onDoubleClick={resetMainAreaRatios}
            />
          ) : null}
          <WorkspaceAuxiliaryPanel
            visible={auxiliaryVisible}
            flexRatio={mainAreaRatios.auxiliary}
            tab={auxiliaryTab}
            apiPort={resolvedApiPort}
            workspaceId={activeSessionWorkspaceId}
            workspaceName={state.workspaceName ?? ""}
            workspaceRoot={state.workspaceRoot ?? ""}
            sessionId={activeSession?.session_id ?? ""}
            activeFilePath={activePreviewPath}
            sessionChangesets={state.sessionChangesets}
            selectedChangesetId={state.selectedChangesetId}
            activeChangeset={state.activeChangeset}
            sessionChangesLoading={state.sessionChangesLoading}
            sessionChangesError={state.sessionChangesError}
            sessionChangesLoadedAt={state.sessionChangesLoadedAt}
            searchOpen={fileTreeSearchOpen}
            collapseVersion={fileTreeCollapseVersion}
            expandedFileTreePaths={expandedFileTreePaths}
            onExpandedFileTreePathsChange={(paths) => {
              if (!activeSessionWorkspaceId) {
                return;
              }
              persistUiSettings((current) => ({
                workspace_file_tree: {
                  expanded_paths_by_workspace: {
                    ...current.workspace_file_tree.expanded_paths_by_workspace,
                    [activeSessionWorkspaceId]: paths,
                  },
                },
              }));
            }}
            onTabChange={handleAuxiliaryTabChange}
            resourcePanel={(
              <ResourcePanel
                resources={state.sessionResources}
                loading={state.sessionResourcesLoading}
                error={state.sessionResourcesError}
                loadedAt={state.sessionResourcesLoadedAt}
                sessionId={activeSession?.session_id ?? ""}
                workspaceId={activeSessionWorkspaceId}
                activePreviewPath={activePreviewPath}
                goal={
                  state.currentGoalSessionId === activeSession?.session_id
                    ? state.currentGoal
                    : null
                }
                goalLoading={state.goalLoading}
                goalError={state.goalError}
                onRefresh={() => {
                  if (activeSession) {
                    void refreshSessionResources(activeSession.session_id);
                  }
                }}
                onRefreshGoal={() => refreshGoal()}
                onUpdateGoal={(payload) => updateGoal(payload)}
                onClearGoal={() => clearGoal()}
                onControl={controlSessionResource}
                onOpenTerminalPreview={workspacePreview.openTerminalPreview}
                onOpenBrowserPreview={workspacePreview.openBrowserPreview}
                onCloseResourcePreview={(kind, resourceId) =>
                  workspacePreview.closeWorkspaceFilePreview(`${kind}://${resourceId}`)
                }
                onShowConversation={showConversation}
                onCreateConnection={async (kind) => {
                  if (!activeSession || !activeSessionWorkspaceId) {
                    throw new Error("新建连接需要当前会话和 Gateway workspace_id");
                  }
                  const created = await createSessionConnection(
                    resolvedApiPort,
                    activeSessionWorkspaceId,
                    activeSession.session_id,
                    kind,
                  );
                  await refreshSessionResources(activeSession.session_id);
                  if (created.kind === "terminal") {
                    workspacePreview.openTerminalPreview(created.resourceId);
                  } else {
                    workspacePreview.openBrowserPreview(created.resourceId);
                  }
                }}
              />
            )}
            onToggleSearch={() => {
              handleAuxiliaryTabChange("files");
              setFileTreeSearchOpen((open) => !open);
            }}
            onCollapseAll={() => {
              handleAuxiliaryTabChange("files");
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
