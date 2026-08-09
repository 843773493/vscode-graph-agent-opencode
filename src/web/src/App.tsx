import AgentStatePanel from "./components/AgentStatePanel";
import BootstrapState from "./components/BootstrapState";
import ChatPanel from "./components/ChatPanel";
import Composer from "./components/composer/Composer";
import EventQueuePanel from "./components/EventQueuePanel";
import AgentSessionsPanel from "./components/AgentSessionsPanel";
import GatewayLogPanel from "./components/GatewayLogPanel";
import RequestLogPanel from "./components/RequestLogPanel";
import ResourcePanel from "./components/ResourcePanel";
import SessionNameDialog from "./components/SessionNameDialog";
import { useWarmConfirm } from "./components/WarmConfirmProvider";
import Toolbar, { type WorkbenchView } from "./components/Toolbar";
import GatewayControlCenter from "./components/workspace/GatewayControlCenter";
import WorkspaceEditorHeader from "./components/workspace/WorkspaceEditorHeader";
import WorkspaceInfoSidebar from "./components/workspace/WorkspaceInfoSidebar";
import WorkspaceFilePreviewArea from "./components/workspace/WorkspaceFilePreviewArea";
import WorkspaceRuntimePreviewArea, {
  type WorkspaceRuntimePreviewTab,
} from "./components/workspace/WorkspaceRuntimePreviewArea";
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
import { useSessionGeneratorResources } from "./hooks/sessionResourceExplorer/useSessionGeneratorResources";
import SessionGeneratorManager from "./components/agentSessions/SessionGeneratorManager";
import { buildSessionCatalogSyncKeys } from "./hooks/sessionResourceExplorer/resourceTreeSync";
import { createSessionConnection } from "./gatewayApi";
import {
  DEFAULT_GATEWAY_PANEL_HEIGHT,
  DEFAULT_MAIN_AREA_RATIOS,
  GATEWAY_PANEL_RESIZING_CLASS,
  LAYOUT_RESIZING_CLASS,
  clampGatewayPanelHeight,
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

const DEFAULT_AUXILIARY_TAB_ORDER: WorkspaceAuxiliaryTab[] = [
  "files",
  "changes",
  "automation",
  "resources",
];

function resolveAuxiliaryTabOrder(
  value: ReadonlyArray<WorkspaceAuxiliaryTab> | null | undefined,
): WorkspaceAuxiliaryTab[] {
  const result: WorkspaceAuxiliaryTab[] = [];
  for (const tab of value ?? []) {
    if (!result.includes(tab)) result.push(tab);
  }
  for (const tab of DEFAULT_AUXILIARY_TAB_ORDER) {
    if (!result.includes(tab)) result.push(tab);
  }
  return result;
}

export default function AppShell() {
  const confirm = useWarmConfirm();
  const {
    state,
    createSession,
    selectSession,
    openWorkspaceSession,
    startNewSessionDraft,
    forkSessionContext,
    renameSession,
    setSessionParent,
    deleteSession,
    refreshSessionChanges,
    refreshSessionResources,
    reviewSessionChangeFile,
    switchContentView,
    controlSessionResource,
    toggleAgentSessionsPanel,
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
    () => state.uiSettings.layout.auxiliary_tab ?? "files",
  );
  const [auxiliaryTabOrder, setAuxiliaryTabOrder] = useState<WorkspaceAuxiliaryTab[]>(
    () => resolveAuxiliaryTabOrder(state.uiSettings.layout.auxiliary_tab_order),
  );
  const [auxiliaryVisible, setAuxiliaryVisible] = useState(
    () => state.uiSettings.layout.auxiliary_visible ?? defaultAuxiliaryVisible(),
  );
  const [panelVisible, setPanelVisible] = useState(
    () => state.uiSettings.layout.panel_visible ?? false,
  );
  const [gatewayPanelHeight, setGatewayPanelHeight] = useState(() =>
    clampGatewayPanelHeight(
      state.uiSettings.layout.panel_height ?? DEFAULT_GATEWAY_PANEL_HEIGHT,
    ),
  );
  const [fileTreeSearchOpen, setFileTreeSearchOpen] = useState(false);
  const [fileTreeCollapseVersion, setFileTreeCollapseVersion] = useState(0);
  const [workspaceInfoVisible, setWorkspaceInfoVisible] = useState({
    automation: true,
  });
  const [markdownSourceVisible, setMarkdownSourceVisible] = useState(false);
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
    const layout = state.uiSettings.layout;
    if (layout.workbench_view) {
      setWorkbenchView(layout.workbench_view);
    }
    if (typeof layout.auxiliary_visible === "boolean") {
      setAuxiliaryVisible(layout.auxiliary_visible);
    }
    if (typeof layout.panel_visible === "boolean") {
      setPanelVisible(layout.panel_visible);
    }
    if (typeof layout.panel_height === "number") {
      setGatewayPanelHeight(clampGatewayPanelHeight(layout.panel_height));
    }
    if (layout.auxiliary_tab) {
      setAuxiliaryTab(layout.auxiliary_tab);
    }
    if (layout.auxiliary_tab_order) {
      setAuxiliaryTabOrder(resolveAuxiliaryTabOrder(layout.auxiliary_tab_order));
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
  const generatorResources = useSessionGeneratorResources(resolvedApiPort);
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
  const activePreviewPath = workspacePreview.activePath;
  const previewTabs = workspacePreview.tabs;
  const previewLoadingPath = workspacePreview.loadingPath;
  const previewError = workspacePreview.error;
  const filePreviewTabs = previewTabs.filter(
    (tab) => tab.previewType === "file" || tab.previewType === "file-placeholder",
  );
  const changePreviewTabs = previewTabs.filter(
    (tab) => tab.previewType === "session-diff",
  );
  const runtimePreviewTabs = previewTabs.filter(
    (tab): tab is WorkspaceRuntimePreviewTab =>
      tab.previewType === "terminal" || tab.previewType === "browser",
  );
  const codePreviewTabs = auxiliaryTab === "changes"
    ? changePreviewTabs
    : filePreviewTabs;
  const activeCodePreviewPath = codePreviewTabs.some(
    (tab) => tab.path === activePreviewPath,
  )
    ? activePreviewPath
    : codePreviewTabs[0]?.path ?? null;
  const activeFilePath = filePreviewTabs.some(
    (tab) => tab.path === activePreviewPath,
  )
    ? activePreviewPath
    : filePreviewTabs[0]?.path ?? null;
  const activeRuntimePreview = runtimePreviewTabs.find(
    (tab) => tab.path === activePreviewPath,
  ) ?? null;
  const codePreviewLoadingPath = codePreviewTabs.some(
    (tab) => tab.path === previewLoadingPath,
  )
    ? previewLoadingPath
    : null;
  const codePreviewError = previewError && (
    (auxiliaryTab === "changes" && activePreviewPath?.startsWith("session-diff://")) ||
    (auxiliaryTab === "files" && activePreviewPath !== null &&
      !activePreviewPath.startsWith("terminal://") &&
      !activePreviewPath.startsWith("browser://") &&
      !activePreviewPath.startsWith("session-diff://"))
  )
    ? previewError
    : null;
  const resourcePanelActive = auxiliaryVisible && auxiliaryTab === "resources";

  const sharedPreviewTab = auxiliaryTab === "files" || auxiliaryTab === "changes";
  const sharedPreviewVisible = sharedPreviewTab && (
    codePreviewTabs.length > 0 ||
    codePreviewLoadingPath !== null ||
    codePreviewError !== null
  );
  const auxiliaryLeftVisible = sharedPreviewTab
    ? sharedPreviewVisible
    : auxiliaryTab === "automation" && workspaceInfoVisible.automation;

  useEffect(() => {
    setMarkdownSourceVisible(false);
  }, [activePreviewPath]);

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
    setStatus(nextVisible ? "右侧侧边栏已切换为展开" : "右侧侧边栏已切换为收起");
  };
  const handleTogglePanel = () => {
    const nextVisible = !panelVisible;
    setPanelVisible(nextVisible);
    persistLayoutSettings({ panel_visible: nextVisible });
    setStatus(nextVisible ? "底部 Gateway 日志面板已切换为展开" : "底部 Gateway 日志面板已切换为收起");
  };
  const handleAuxiliaryTabChange = (tab: WorkspaceAuxiliaryTab) => {
    setAuxiliaryTab(tab);
    persistLayoutSettings({ auxiliary_tab: tab });
  };

  const handleAuxiliaryTabReorder = (tabOrder: WorkspaceAuxiliaryTab[]) => {
    const nextOrder = resolveAuxiliaryTabOrder(tabOrder);
    setAuxiliaryTabOrder(nextOrder);
    persistLayoutSettings({ auxiliary_tab_order: nextOrder });
  };
  const openAuxiliaryTab = (tab: WorkspaceAuxiliaryTab) => {
    setAuxiliaryVisible(true);
    setAuxiliaryTab(tab);
    persistLayoutSettings({ auxiliary_visible: true, auxiliary_tab: tab });
  };
  const handleOpenChangesView = () => {
    setAuxiliaryVisible(true);
    setAuxiliaryTab("changes");
    persistLayoutSettings({ auxiliary_visible: true, auxiliary_tab: "changes" });
  };
  const startLayoutResize = (
    target: LayoutResizeTarget,
    event: ReactPointerEvent<HTMLButtonElement>,
  ) => {
    event.preventDefault();
    cleanupLayoutResizeRef.current?.();

    const startX = event.clientX;
    const startRatios = mainAreaRatios;
    const effectiveStartRatios = startRatios;
    const [left, leftSelector, right, rightSelector]: [
      MainAreaKey,
      string,
      MainAreaKey,
      string,
    ] = target === "agent-sessions-right"
      ? ["agent_sessions", ".agent-sessions-panel", "chat", ".workbench-main-column"]
      : target === "workspace-editor-left"
        ? ["chat", ".sessions-part-card", "workspace_preview", ".workspace-editor-shell"]
        : ["workspace_preview", ".workspace-preview-panel", "auxiliary", ".auxiliary-panel"];
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
      const resizedRatios = target === "agent-sessions-right"
        ? (() => {
            const combinedWidth = leftWidth + rightWidth;
            if (combinedWidth <= 0) {
              throw new Error("无法调整没有宽度的会话侧栏和主工作区");
            }
            const nextSidebarWidth = leftWidth + deltaX;
            const nextMainWidth = rightWidth - deltaX;
            if (nextSidebarWidth <= 0 || nextMainWidth <= 0) {
              return effectiveStartRatios;
            }
            const mainRatio =
              effectiveStartRatios.chat +
              effectiveStartRatios.workspace_preview +
              effectiveStartRatios.auxiliary;
            const combinedRatio = effectiveStartRatios.agent_sessions + mainRatio;
            const nextMainRatio = combinedRatio * (nextMainWidth / combinedWidth);
            const mainScale = nextMainRatio / mainRatio;
            return {
              ...effectiveStartRatios,
              agent_sessions: combinedRatio * (nextSidebarWidth / combinedWidth),
              chat: effectiveStartRatios.chat * mainScale,
              workspace_preview:
                effectiveStartRatios.workspace_preview * mainScale,
              auxiliary: effectiveStartRatios.auxiliary * mainScale,
            };
          })()
        : target === "workspace-editor-left"
          ? (() => {
              const combinedWidth = leftWidth + rightWidth;
              if (combinedWidth <= 0) {
                throw new Error("无法调整没有宽度的会话区和编辑器工作区");
              }
              const nextChatWidth = leftWidth + deltaX;
              const nextEditorWidth = rightWidth - deltaX;
              if (nextChatWidth <= 0 || nextEditorWidth <= 0) {
                return effectiveStartRatios;
              }
              const editorRatio =
                effectiveStartRatios.workspace_preview +
                effectiveStartRatios.auxiliary;
              const combinedRatio = effectiveStartRatios.chat + editorRatio;
              const nextChatRatio = combinedRatio * (nextChatWidth / combinedWidth);
              const nextEditorRatio = combinedRatio * (nextEditorWidth / combinedWidth);
              const editorScale = nextEditorRatio / editorRatio;
              return {
                ...effectiveStartRatios,
                chat: nextChatRatio,
                workspace_preview:
                  effectiveStartRatios.workspace_preview * editorScale,
                auxiliary: effectiveStartRatios.auxiliary * editorScale,
              };
            })()
        : resizeAdjacentMainAreas({
            ratios: effectiveStartRatios,
            left,
            right,
            leftWidth,
            rightWidth,
            deltaX,
          });
      latestRatios = resizedRatios;
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
  const startGatewayPanelResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    cleanupLayoutResizeRef.current?.();

    const startY = event.clientY;
    const startHeight = gatewayPanelHeight;
    let latestHeight = startHeight;
    let moved = false;

    const handlePointerMove = (moveEvent: PointerEvent) => {
      const deltaY = startY - moveEvent.clientY;
      if (deltaY === 0) {
        return;
      }
      moved = true;
      latestHeight = clampGatewayPanelHeight(startHeight + deltaY);
      setGatewayPanelHeight(latestHeight);
    };

    const finishResize = () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", finishResize);
      window.removeEventListener("pointercancel", finishResize);
      document.body.classList.remove(GATEWAY_PANEL_RESIZING_CLASS);
      cleanupLayoutResizeRef.current = null;
      if (moved) {
        persistLayoutSettings({ panel_height: latestHeight });
      }
    };

    document.body.classList.add(GATEWAY_PANEL_RESIZING_CLASS);
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", finishResize);
    window.addEventListener("pointercancel", finishResize);
    cleanupLayoutResizeRef.current = finishResize;
  };
  const resetGatewayPanelHeight = () => {
    setGatewayPanelHeight(DEFAULT_GATEWAY_PANEL_HEIGHT);
    persistLayoutSettings({ panel_height: DEFAULT_GATEWAY_PANEL_HEIGHT });
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
    await openWorkspaceSession(workspaceId, sessionId);
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
  const renderContentView = () => {
    if (state.error) {
      return (
        <div className="empty-state error-state">
          <div className="error-title">前端初始化失败</div>
          <div className="error-message">{state.error}</div>
          <button
            type="button"
            className="error-retry-button"
            onClick={() => void refreshGatewayState().catch(() => undefined)}
          >
            重新加载工作区
          </button>
        </div>
      );
    }

    if (state.isBootstrapping) {
      return <BootstrapState onRetry={refreshGatewayState} />;
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
      onOpen={(content, reference) => {
        openAuxiliaryTab("files");
        workspacePreview.openWorkspaceFileReference(content, reference);
      }}
    >
      <div
      className={`app-shell agent-sessions-workbench shell-gradient-background ${agentSessionsVisible ? "agent-sessions-open" : "agent-sessions-closed"}`}
      data-agent-sessions-open={String(agentSessionsVisible)}
      data-bt-surface="canvas"
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
        agentSessionsVisible={agentSessionsVisible}
        onToggleAgentSessionsPanel={toggleAgentSessionsPanel}
        panelVisible={panelVisible}
        onTogglePanel={handleTogglePanel}
        workbenchView={workbenchView}
        onWorkbenchViewChange={handleWorkbenchViewChange}
        showAuxiliaryToggle={workbenchView === "sessions"}
      />
      <div className="workbench-body">
        <AgentSessionsPanel
          apiPort={resolvedApiPort}
          sessions={sortedSessions}
          currentSessionId={
            state.currentSessionWorkspaceId === state.activeGatewayWorkspaceId
              ? activeSession?.session_id ?? ""
              : ""
          }
          onSelectSession={selectSession}
          onRenameSession={handleRenameSession}
          onDeleteSession={handleDeleteSession}
          onSetSessionParent={handleSetSessionParent}
          onForkSessionContext={handleForkSessionContext}
          onStatusChange={setStatus}
          isOpen={agentSessionsVisible && workbenchView === "sessions"}
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
          onReconnectWorkspace={reconnectGatewayWorkspace}
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
          onInvalidateSessionCatalog={invalidateSessionCatalog}
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
          generatorResources={generatorResources}
        />
        {agentSessionsVisible && workbenchView === "sessions" ? (
          <button
            type="button"
            className="layout-sash layout-sash-agent-sessions-right"
            title="拖拽调整会话侧栏宽度，双击还原"
            aria-label="调整会话侧栏宽度"
            onPointerDown={(event) => startLayoutResize("agent-sessions-right", event)}
            onDoubleClick={resetMainAreaRatios}
          />
        ) : null}
        <div
          className="workbench-main-column"
          style={{
            flexBasis: 0,
            flexGrow:
              mainAreaRatios.chat +
              mainAreaRatios.workspace_preview +
              mainAreaRatios.auxiliary,
          }}
        >
      <div
        className={`gateway-view-slot${
          workbenchView === "gateway" ? "" : " preserve-mounted-hidden"
        }`}
        hidden={workbenchView !== "gateway"}
        data-bt-surface="layout"
      >
        <GatewayControlCenter
          apiPort={resolvedApiPort}
          workspaces={state.gatewayWorkspaces}
          gatewayError={state.gatewayError}
          onAddSsh={addSshGatewayWorkspace}
          onRefresh={refreshGatewayState}
          onReconnect={reconnectGatewayWorkspace}
          uiSettings={state.uiSettings}
          onUpdateUiSettings={updateUiSettings}
        />
      </div>
      <main
        className={`content sessions-workbench-grid${
          workbenchView === "sessions" ? "" : " preserve-mounted-hidden"
        }`}
        hidden={workbenchView !== "sessions"}
        data-bt-surface="layout"
      >
        <div
          className={`content-layout${auxiliaryVisible ? "" : " auxiliary-collapsed"}`}
        >
          <section
            className="chat-panel sessions-part-card"
            data-bt-surface="workspace"
            style={{ flexBasis: 0, flexGrow: mainAreaRatios.chat }}
          >
            <div className="session-view-surface">
              <div className="session-view-content">{renderContentView()}</div>
              <Composer />
            </div>
          </section>
          {auxiliaryVisible ? (
            <>
              <button
                type="button"
                className="layout-sash layout-sash-workspace-editor-left"
                title="拖拽调整会话区与编辑器工作区宽度，双击还原"
                aria-label="调整会话区与编辑器工作区宽度"
                onPointerDown={(event) => startLayoutResize("workspace-editor-left", event)}
                onDoubleClick={resetMainAreaRatios}
              />
              <section
                className="workspace-editor-shell"
                data-bt-surface="workspace"
                style={{
                  flexBasis: 0,
                  flexGrow:
                    mainAreaRatios.workspace_preview +
                    mainAreaRatios.auxiliary,
                }}
              >
                <WorkspaceEditorHeader
                  auxiliaryTab={auxiliaryTab}
                  tabOrder={auxiliaryTabOrder}
                  onSelectAuxiliaryTab={openAuxiliaryTab}
                  onReorderAuxiliaryTabs={handleAuxiliaryTabReorder}
                />
                <div className={`workspace-editor-body workspace-editor-body-${auxiliaryTab}`}>
                  {sharedPreviewTab ? (
                    sharedPreviewVisible ? (
                      <WorkspaceFilePreviewArea
                        context={auxiliaryTab === "changes" ? "changes" : "files"}
                        visible
                        flexRatio={mainAreaRatios.workspace_preview}
                        apiPort={resolvedApiPort}
                        workspaceId={activeSessionWorkspaceId}
                        workspaceName={state.workspaceName ?? "未选择工作区"}
                        sessionTitle={activeSession?.title ?? "新会话"}
                        tabs={codePreviewTabs}
                        activePath={activeCodePreviewPath}
                        loadingPath={codePreviewLoadingPath}
                        error={codePreviewError}
                        editingPath={workspacePreview.editingPath}
                        draftContent={workspacePreview.draftContent}
                        savingPath={workspacePreview.savingPath}
                        hasUnsavedEdit={workspacePreview.hasUnsavedEdit}
                        markdownSourceVisible={markdownSourceVisible}
                        onMarkdownSourceChange={setMarkdownSourceVisible}
                        onBeginEdit={workspacePreview.beginWorkspaceFileEdit}
                        onDraftChange={workspacePreview.setDraftContent}
                        onCancelEdit={() => void workspacePreview.cancelWorkspaceFileEdit()}
                        onSaveEdit={workspacePreview.saveWorkspaceFileEdit}
                        onOpenWorkspacePath={workspacePreview.openWorkspaceFilePath}
                      />
                    ) : null
                  ) : auxiliaryTab === "automation" ? (
                    <WorkspaceInfoSidebar
                      tab="automation"
                      visible={workspaceInfoVisible.automation}
                      flexRatio={mainAreaRatios.workspace_preview}
                      workspaceName={state.workspaceName ?? "未选择工作区"}
                      workspaceRoot={state.workspaceRoot ?? ""}
                      sessionTitle={activeSession?.title ?? "新会话"}
                      onToggle={() => setWorkspaceInfoVisible((current) => ({
                        ...current,
                        automation: !current.automation,
                      }))}
                    />
                  ) : null}
                  {auxiliaryLeftVisible ? (
                    <button
                      type="button"
                      className="layout-sash layout-sash-auxiliary-left"
                      title="拖拽调整左侧信息区宽度，双击还原"
                      aria-label="调整左侧信息区宽度"
                      onPointerDown={(event) => startLayoutResize("auxiliary-left", event)}
                      onDoubleClick={resetMainAreaRatios}
                    />
                  ) : null}
                  <WorkspaceAuxiliaryPanel
                    visible={auxiliaryVisible}
                    flexRatio={sharedPreviewTab && !sharedPreviewVisible
                      ? mainAreaRatios.workspace_preview + mainAreaRatios.auxiliary
                      : mainAreaRatios.auxiliary}
                    tab={auxiliaryTab}
                    apiPort={resolvedApiPort}
                    workspaceId={activeSessionWorkspaceId}
                    workspaceName={state.workspaceName ?? ""}
                    workspaceRoot={state.workspaceRoot ?? ""}
                    sessionId={activeSession?.session_id ?? ""}
                    sessionTitle={activeSession?.title ?? "新会话"}
                    activeFilePath={activeFilePath}
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
                    automationPanel={(
                      <SessionGeneratorManager
                        apiPort={resolvedApiPort}
                        generatorResources={generatorResources}
                        workspaces={state.gatewayWorkspaces}
                        activeWorkspaceId={state.activeGatewayWorkspaceId}
                        currentSessionId={activeSession?.session_id ?? ""}
                        onStatusChange={setStatus}
                        onOpenConnectionManager={() => handleWorkbenchViewChange("gateway")}
                        onReconnectWorkspace={reconnectGatewayWorkspace}
                        onStartWorkspace={startManagedGatewayWorkspaceBackend}
                      />
                    )}
                    resourcePanel={(
                      <ResourcePanel
                        resources={state.sessionResources}
                        loading={state.sessionResourcesLoading}
                        error={state.sessionResourcesError}
                        loadedAt={state.sessionResourcesLoadedAt}
                        sessionId={activeSession?.session_id ?? ""}
                        workspaceId={activeSessionWorkspaceId}
                        activePreviewPath={activeRuntimePreview?.path ?? null}
                        onRefresh={() => {
                          if (activeSession) {
                            void refreshSessionResources(activeSession.session_id);
                          }
                        }}
                        onControl={controlSessionResource}
                        onOpenTerminalPreview={(terminalId) => {
                          openAuxiliaryTab("resources");
                          workspacePreview.openTerminalPreview(terminalId);
                        }}
                        onOpenBrowserPreview={(browserId) => {
                          openAuxiliaryTab("resources");
                          workspacePreview.openBrowserPreview(browserId);
                        }}
                        onCloseResourcePreview={(kind, resourceId) =>
                          workspacePreview.closeWorkspaceFilePreview(`${kind}://${resourceId}`)
                        }
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
                            openAuxiliaryTab("resources");
                            workspacePreview.openTerminalPreview(created.resourceId);
                          } else {
                            openAuxiliaryTab("resources");
                            workspacePreview.openBrowserPreview(created.resourceId);
                          }
                        }}
                      />
                    )}
                    runtimePreview={activeRuntimePreview ? (
                      <WorkspaceRuntimePreviewArea
                        tab={activeRuntimePreview}
                        onClose={() => void workspacePreview.closeWorkspaceFilePreview(activeRuntimePreview.path)}
                      />
                    ) : null}
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
                    onOpenFile={(node) => {
                      openAuxiliaryTab("files");
                      workspacePreview.openWorkspaceFilePreview(node);
                    }}
                    onStatusChange={setStatus}
                  />
                </div>
              </section>
            </>
          ) : null}
        </div>
      </main>
      {panelVisible ? (
        <>
          <button
            type="button"
            className="layout-sash layout-sash-gateway-panel"
            title="拖拽调整 Gateway 日志面板高度，双击还原"
            aria-label="调整 Gateway 日志面板高度"
            onPointerDown={startGatewayPanelResize}
            onDoubleClick={resetGatewayPanelHeight}
          />
          <GatewayLogPanel
            apiPort={resolvedApiPort}
            workspaces={state.gatewayWorkspaces}
            height={gatewayPanelHeight}
            onClose={handleTogglePanel}
          />
        </>
      ) : null}
        </div>
      </div>
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
