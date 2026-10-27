import { requireForkTargetWorkspace, requireGatewayWorkspaceRequestTarget } from "./api/workspaceIdentity";
import { getGatewayHealth } from "./gatewayApi";
import { useManagedBackendConfirmation } from "./hooks/gatewayWorkspace/useManagedBackendConfirmation";
import { useWorkspaceTerminalResources } from "./hooks/workspace/useWorkspaceTerminalResources";
import {
  closeChildConversationAndClearForkSelection,
  createTerminalFromResourcePanel,
} from "./hooks/workspace/createTerminalFromResourcePanel";
import { controlWorkspaceResource, getWorkspaceResource } from "./api/workspaceResources";
import { getSessionMainThread } from "./api/session/sessions";
import WorkbenchStatusBar from "./components/WorkbenchStatusBar";
import PendingQueueBar from "./components/chat/pending/PendingQueueBar";
import Composer from "./components/composer/Composer";
import AgentSessionsPanel from "./components/panels/sessionPanels/AgentSessionsPanel";
import ResourcePanel from "./components/panels/resourceTree/ResourcePanel";
import GoalManagerCard from "./components/cards/GoalManagerCard";
import ChildThreadPanel from "./components/panels/sessionPanels/ChildThreadPanel";
import GatewayExtensionResourcePanel from "./components/panels/resourceTree/GatewayExtensionResourcePanel";
import SessionNameDialog from "./components/overlays/SessionNameDialog";
import SessionContextForkDialog from "./components/overlays/SessionContextForkDialog";
import { useWarmConfirm } from "./components/shell/WarmConfirmProvider";
import Toolbar, { type WorkbenchView } from "./components/shell/Toolbar";
import ContentViewSlots from "./components/shell/ContentViewSlots";
import WorkbenchBottomPanel from "./components/shell/WorkbenchBottomPanel";
import GatewayControlCenter from "./components/workspace/gateway/GatewayControlCenter";
import WorkspaceEditorHeader from "./components/workspace/WorkspaceEditorHeader";
import WorkspaceFilePreviewArea from "./components/workspace/WorkspaceFilePreviewArea";
import NodeDebugWorkbench from "./components/nodeDebug/NodeDebugWorkbench";
import WorkspaceRuntimePreviewArea from "./components/workspace/WorkspaceRuntimePreviewArea";
import { WorkspaceFileReferenceProvider } from "./components/workspace/WorkspaceFileReferenceContext";
import WorkspaceAuxiliaryPanel from "./components/workspace/WorkspaceAuxiliaryPanel";
import WorkspaceAttachmentPreview from "./components/workspace/preview/WorkspaceAttachmentPreview";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  DEFAULT_BACKEND_PORT,
} from "./api";
import {
  useAppState,
} from "./hooks";
import { useWorkspacePreviewTabs } from "./hooks/workspace/useWorkspacePreviewTabs";
import { useMainAreaResize } from "./hooks/panel/useMainAreaResize";
import { useBottomPanelResize } from "./hooks/panel/useBottomPanelResize";
import { useNodeDebugWorkbench } from "./hooks/nodeDebug/useNodeDebugWorkbench";
import { useChildThreadLoader } from "./hooks/session/useChildThreadLoader";
import { resolveTerminalResourceTarget, useChildThreadConversation } from "./hooks/sessionTurnHistory/useChildThreadConversation";
import { useGatewayExtensionResources } from "./hooks/gatewayExtensions/useGatewayExtensionResources";
import { useGatewayExtensionWindow } from "./hooks/gatewayExtensions/useGatewayExtensionWindow";
import { useWorkbenchPanelRouting } from "./hooks/panel/useWorkbenchPanelRouting";
import { useSessionCatalogActions } from "./hooks/shell/useSessionCatalogActions";
import { useWorkbenchLayoutPreferences } from "./hooks/shell/useWorkbenchLayoutPreferences";
import { useSessionChangesPreview } from "./hooks/shell/useSessionChangesPreview";
import { projectWorkspacePreviewTabs } from "./hooks/shell/previewTabProjection";
import { createSessionConnection } from "./api/gateway/sessionConnections";
import {
  DEFAULT_MAIN_AREA_RATIOS,
} from "./layout/workbenchLayout";
import { sessionScopeKey } from "./state/session/sessionScope";
import type { OutputDiagnostic } from "./state/workspaceBottomPanel";
import { getConversationsForSession } from "./state/conversations";
import { resolveAgentSessionsPreferences } from "./state/uiSettings/preferences";
import {
  resolveExtensionWindowRequest,
} from "./utils/extensionResourceWindow";
import type {
  AttachmentRef,
} from "./types/backend";
import type { ResourceThreadRef } from "./api/workspaceResources";

export default function AppShell() {
  const confirm = useWarmConfirm();
  const extensionWindowRequest = useMemo(resolveExtensionWindowRequest, []);
  const extensionWindowRequested = extensionWindowRequest !== null;
  const {
    state,
    createSession,
    selectSession,
    openWorkspaceSession,
    forkSessionContext,
    readSessionSelectionIntent,
    renameSession,
    setSessionParent,
    deleteSession,
    loadSessionChangesets,
    refreshSessionChanges,
    refreshSessionResources,
    refreshGoal,
    updateGoal,
    clearGoal,
    reviewSessionChangeFile,
    switchContentView,
    controlSessionResource,
    toggleAgentSessionsPanel,
    activateGatewayWorkspace,
    refreshGatewayState,
    reconnectGatewayWorkspace,
    startManagedGatewayWorkspaceBackend: startBackend,
    stopManagedGatewayWorkspaceBackend: stopBackend,
    safeRestartManagedGatewayWorkspaceBackend: safeRestartBackend,
    forceRestartManagedGatewayWorkspaceBackend: forceRestartBackend,
    addManagedGatewayWorkspace,
    addSshGatewayWorkspace,
    removeGatewayWorkspace,
    renameGatewayWorkspace,
    setGatewayWorkspaceParent,
    refreshGatewayWorkspaceSessions,
    copySessionInformation,
    copyWorkspaceInformation,
    updateUiSettings,
    saveSessionViewState,
    setStatus,
    sessionCatalogOutboxRuntime,
    sessionCatalogOutboxSnapshot,
    replayTurn,
    removePendingRequest,
    clearPendingRequests,
    updatePendingRequestPolicy,
    loadAroundTurn,
    loadNewerMessages,
    loadOlderMessages,
    loadTurnDetails,
    loadAgentStateMessageRawContent,
    refreshTurnHistory,
    loadOlderTraceHistory,
    refreshTraceHistory,
    refreshLLMRequestLogs,
  } = useAppState();
  const loadToolDetails = useCallback(
    (turnId: string, toolCallId: string) => loadTurnDetails(
      [turnId],
      `tool-details:${turnId}:${toolCallId}`,
      false,
      ["tool_call", "tool_result"],
      [toolCallId],
    ),
    [loadTurnDetails],
  );
  const [fileTreeSearchOpen, setFileTreeSearchOpen] = useState(false);
  const [fileTreeCollapseVersion, setFileTreeCollapseVersion] = useState(0);
  const [fileTreeSettingsVersion, setFileTreeSettingsVersion] = useState(0);
  const [markdownSourceVisible, setMarkdownSourceVisible] = useState(false);
  const [selectedAttachmentPreview, setSelectedAttachmentPreview] = useState<{
    sessionId: string;
    attachment: AttachmentRef;
  } | null>(null);
  const activeSession = state.currentSession;
  const activeSessionWorkspaceId =
    state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId;
  // 内容视图槽既承载有会话时的对话内容，也承载外壳级的工作区初始化失败出口；
  // 初始化失败恰恰发生在还没有会话的时候，所以错误出口不能和会话内容挤在同一个
  // activeSession 分支里 —— 否则它唯一的设计场景永远不可达，而别的动作失败会误用它。
  // 首屏加载同理：还没有会话时骨架态也必须可达，否则用户只能看到空白，
  // 既没有进度文案也没有超时后的重试入口。
  const contentViewSlotsVisible =
    Boolean(activeSession) || Boolean(state.error) || state.isBootstrapping;
  useEffect(() => {
    if (
      selectedAttachmentPreview
      && selectedAttachmentPreview.sessionId !== activeSession?.session_id
    ) {
      setSelectedAttachmentPreview(null);
    }
  }, [activeSession?.session_id, selectedAttachmentPreview]);
  const bottomPanelWorkspaceId = state.activeGatewayWorkspaceId ?? activeSessionWorkspaceId;
  const [outputDiagnostic, setOutputDiagnostic] = useState<{
    workspaceId: string | null;
    diagnostic: OutputDiagnostic;
  } | null>(null);
  const bottomPanelWorkspace = useMemo(
    () => state.gatewayWorkspaces.find(
      (workspace) => workspace.workspace_id === bottomPanelWorkspaceId,
    ) ?? null,
    [bottomPanelWorkspaceId, state.gatewayWorkspaces],
  );
  const {
    workbenchView,
    handleWorkbenchViewChange,
    auxiliaryTab,
    setAuxiliaryTab,
    auxiliaryTabOrder,
    setAuxiliaryTabOrder,
    auxiliaryVisible,
    setAuxiliaryVisible,
    chatVisible,
    setChatVisible,
    mainAreaRatios,
    setMainAreaRatios,
    bottomPanelState,
    panelVisible,
    setWorkspaceBottomPanelStates,
    extensionWindowFallback,
    setExtensionWindowFallback,
    persistUiSettings,
    persistLayoutSettings,
    updateBottomPanelState,
  } = useWorkbenchLayoutPreferences({
    uiSettings: state.uiSettings,
    extensionWindowRequest,
    bottomPanelWorkspaceId,
    updateUiSettings,
    setStatus,
  });
  const openOutputDiagnostic = (title: string, detail: string) => {
    setOutputDiagnostic({ workspaceId: bottomPanelWorkspaceId, diagnostic: { title, detail } });
    // 查看错误是本地展示动作；断网时也必须能打开，不能等待布局偏好写入成功。
    setWorkspaceBottomPanelStates((previous) => ({
      ...previous,
      [bottomPanelWorkspaceId ?? ""]: { ...bottomPanelState, visible: true, tab: "output" },
    }));
  };
  const activeSessionCacheKey =
    activeSession && activeSessionWorkspaceId
      ? sessionScopeKey(activeSessionWorkspaceId, activeSession.session_id)
      : activeSession?.session_id ?? null;
  const activeTurnTimeline = activeSessionCacheKey
    ? state.turnTimelinesBySession.get(activeSessionCacheKey) ?? null
    : null;
  const currentActiveJobId = activeSessionCacheKey
    ? state.activeJobIdsBySession.get(activeSessionCacheKey) ?? null
    : null;
  const agentSessionsPreferences = useMemo(
    () => resolveAgentSessionsPreferences(state.uiSettings),
    [state.uiSettings],
  );
  const expandedFileTreePaths = useMemo(() => {
    if (!activeSessionWorkspaceId) {
      return [""];
    }
    return state.uiSettings.workspace_file_tree.expanded_paths_by_workspace[
      activeSessionWorkspaceId
    ] ?? [""];
  }, [activeSessionWorkspaceId, state.uiSettings.workspace_file_tree]);

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
      state.messageStreamsByTurnStream,
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
  const {
    start: startManagedGatewayWorkspaceBackend,
    stop: stopManagedGatewayWorkspaceBackend,
    safeRestart: safeRestartManagedGatewayWorkspaceBackend,
    forceRestart: forceRestartManagedGatewayWorkspaceBackend,
  } = useManagedBackendConfirmation({
    apiPort: resolvedApiPort, onStart: startBackend, onStop: stopBackend,
    onSafeRestart: safeRestartBackend, onForceRestart: forceRestartBackend,
    onRefresh: refreshGatewayState,
  });
  const sortedSessions = useMemo(
    () => [...state.sessions].sort(
      (a, b) =>
        new Date(b.updated_at || b.created_at || "").getTime() -
        new Date(a.updated_at || a.created_at || "").getTime(),
    ),
    [state.sessions],
  );
  const {
    handleToggleAuxiliaryPanel,
    handleToggleChatPanel,
    handleTogglePanel,
    handleAuxiliaryTabChange,
    handleAuxiliaryTabReorder,
    openAuxiliaryTab,
    openTerminalPanel,
  } = useWorkbenchPanelRouting({
    auxiliaryVisible,
    setAuxiliaryVisible,
    chatVisible,
    setChatVisible,
    setAuxiliaryTab,
    setAuxiliaryTabOrder,
    bottomPanelState,
    updateBottomPanelState,
    bottomPanelWorkspaceId,
    extensionWindowRequested,
    setExtensionWindowFallback,
    persistLayoutSettings,
    setStatus,
  });
  const agentSessionsVisible = state.agentSessionsPanelOpen;
  const workspacePreview = useWorkspacePreviewTabs({
    apiPort: resolvedApiPort,
    workspaceId: activeSessionWorkspaceId,
    settingsLoaded: state.uiSettingsLoaded,
    restoredLayout: state.uiSettings.layout,
    onPersistLayout: persistLayoutSettings,
    onStatusChange: setStatus,
  });
  const {
    threadId: nodeDebugThreadId,
    mainThreadId: nodeDebugMainThreadId,
    controller: nodeDebugController,
    activeFrame: nodeDebugActiveFrame,
    selectThread: selectDebugThread,
    changeBreakpoint: changeNodeDebugBreakpoint,
  } = useNodeDebugWorkbench({
    apiPort: resolvedApiPort,
    workspaceId: activeSessionWorkspaceId,
    workspaces: state.gatewayWorkspaces,
    sessionId: activeSession?.session_id ?? null,
    enabled: auxiliaryVisible && auxiliaryTab === "debug",
    onStatusChange: setStatus,
    extensionWindowRequested,
    setAuxiliaryVisible,
    setAuxiliaryTab,
    persistLayoutSettings,
  });
  const activeMainResourceThread: ResourceThreadRef | null = activeSession && nodeDebugMainThreadId
    ? { session_id: activeSession.session_id, thread_id: nodeDebugMainThreadId } : null;
  const extensionResourceKey = extensionWindowRequest?.workspaceId &&
    extensionWindowRequest.sessionId &&
    extensionWindowRequest.kind &&
    extensionWindowRequest.resourceId
    ? `${extensionWindowRequest.workspaceId}:${extensionWindowRequest.sessionId}:${extensionWindowRequest.kind}:${extensionWindowRequest.resourceId}`
    : null;
  const extensionResources = useGatewayExtensionResources({
    apiPort: resolvedApiPort,
    initialResourceKey: extensionResourceKey,
    enabled:
      extensionWindowRequested
      || extensionWindowFallback
,
  });
  const workspaceTerminals = useWorkspaceTerminalResources(
    resolvedApiPort, bottomPanelWorkspaceId,
    panelVisible && bottomPanelState.tab === "terminal",
  );
  const activePreviewPath = workspacePreview.activePath;
  const {
    filePreviewTabs,
    codePreviewTabs,
    activeCodePreviewPath,
    activeFilePath,
    activeRuntimePreview,
    codePreviewLoadingPath,
    codePreviewError,
  } = useMemo(
    () => projectWorkspacePreviewTabs({
      tabs: workspacePreview.tabs,
      auxiliaryTab,
      activePath: workspacePreview.activePath,
      loadingPath: workspacePreview.loadingPath,
      error: workspacePreview.error,
    }),
    [
      auxiliaryTab,
      workspacePreview.activePath,
      workspacePreview.error,
      workspacePreview.loadingPath,
      workspacePreview.tabs,
    ],
  );

  useEffect(() => {
    if (!activeRuntimePreview && !extensionWindowRequested) {
      setExtensionWindowFallback(false);
    }
  }, [activeRuntimePreview, extensionWindowRequested]);

  useEffect(() => {
    if (!extensionWindowRequested) {
      return;
    }
    setAuxiliaryVisible(true);
    setAuxiliaryTab(extensionWindowRequest?.kind === "debug" ? "debug" : "resources");
  }, [extensionWindowRequest?.kind, extensionWindowRequested]);
  const resourcePanelActive =
    auxiliaryVisible
    && auxiliaryTab === "resources"
    && state.gatewayUserAccess !== null;

  // 右侧侧边栏「运行与连接」标签中的子会话线程面板：会话层级资源。
  const childThreadPanelActive = resourcePanelActive;
  const [forkChildOwner, setForkChildOwner] = useState<{
    workspaceId: string; sessionId: string; mainThreadId: string | null; initialThreadSelection: string | null;
  } | null>(null);
  useEffect(() => { setForkChildOwner(null); }, [activeSession?.session_id, activeSessionWorkspaceId]);
  const childOwnerWorkspaceId = forkChildOwner?.workspaceId ?? activeSessionWorkspaceId;
  const childOwnerSessionId = forkChildOwner?.sessionId ?? activeSession?.session_id ?? null;
  const childOwnerWorkspace = state.gatewayWorkspaces.find((workspace) => workspace.workspace_id === childOwnerWorkspaceId);
  const childOwnerSession = childOwnerWorkspaceId
    ? state.sessionsByWorkspace.get(childOwnerWorkspaceId)?.find((session) => session.session_id === childOwnerSessionId)
    : null;
  const childOwnerLabel = childOwnerSessionId
    ? `${childOwnerWorkspace?.connection_kind === "local" ? "本机 Gateway" : childOwnerWorkspace?.remote?.gateway_id ?? "未登记 Gateway"} · ${childOwnerWorkspace?.name ?? childOwnerWorkspaceId} · ${childOwnerSession?.title ?? childOwnerSessionId}`
    : "尚未选择会话";
  const childThreads = useChildThreadLoader({
    apiPort: resolvedApiPort,
    workspaceId: childOwnerWorkspaceId,
    sessionId: childOwnerSessionId,
    enabled: childThreadPanelActive,
  });
  const childConversation = useChildThreadConversation({
    apiPort: resolvedApiPort,
    workspaceId: childOwnerWorkspaceId,
    sessionId: childOwnerSessionId,
    threads: childThreads.threads,
    enabled: childThreadPanelActive,
  });
  useEffect(() => {
    if (!forkChildOwner?.initialThreadSelection) return;
    void childThreads.refresh();
    childConversation.selectThreadForOwner(forkChildOwner, forkChildOwner.initialThreadSelection);
    setForkChildOwner((current) => current === forkChildOwner
      ? { ...current, initialThreadSelection: null }
      : current);
  }, [forkChildOwner, childThreads.refresh, childConversation.selectThreadForOwner]);
  const childSelectionIntent = childConversation.selectionIntent ?? (
    forkChildOwner?.initialThreadSelection && childOwnerWorkspaceId && childOwnerSessionId
      ? {
          workspaceId: childOwnerWorkspaceId,
          sessionId: childOwnerSessionId,
          threadId: forkChildOwner.initialThreadSelection,
          view: "history" as const,
        }
      : null
  );
  const terminalResourceTarget = resolveTerminalResourceTarget({
    childPanelVisible: childThreadPanelActive && !extensionWindowRequested,
    selectionIntent: childSelectionIntent,
    selectedThread: childConversation.selectedThread,
    bottomPanelWorkspaceId,
    mainThread: activeSessionWorkspaceId === bottomPanelWorkspaceId ? activeMainResourceThread : null,
  });

  const sharedPreviewTab = auxiliaryTab === "files" || auxiliaryTab === "changes" || (
    auxiliaryTab === "debug" && (extensionWindowRequested || extensionWindowFallback)
  );
  const sharedPreviewVisible = sharedPreviewTab && (
    codePreviewTabs.length > 0 ||
    codePreviewLoadingPath !== null ||
    codePreviewError !== null
  );
  const auxiliaryLeftVisible = sharedPreviewTab && sharedPreviewVisible;
  const debugPanel = (
    <NodeDebugWorkbench
      apiPort={resolvedApiPort}
      workspaceId={activeSessionWorkspaceId}
      sessionId={activeSession?.session_id ?? null}
      threadId={nodeDebugThreadId}
      mainThreadId={nodeDebugMainThreadId}
      activeFilePath={activeFilePath}
      nodeDebugController={nodeDebugController}
      sessions={sortedSessions}
      compact={!extensionWindowRequested && !extensionWindowFallback}
      onOpenExtensionWindow={() => openExtensionWindow("debug")}
      onSelectThread={selectDebugThread}
      onOpenWorkspacePath={workspacePreview.openWorkspaceFilePath}
      onStatusChange={setStatus}
    />
  );

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
    const resourceSessionId = activeSession?.session_id ?? null;
    if (!resourcePanelActive || !resourceSessionId) {
      return;
    }

    let disposed = false;
    let pollInFlight = false;
    const poll = async (silent: boolean) => {
      if (
        disposed
        || pollInFlight
        || (silent && document.visibilityState !== "visible")
      ) {
        return;
      }
      pollInFlight = true;
      try {
        const result = await refreshSessionResources(resourceSessionId, { silent });
        if (result === "failed" && !silent) {
          setStatus("后台连接刷新失败，请查看运行与连接中的错误详情");
        }
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
  }, [
    activeSession?.session_id,
    activeSessionWorkspaceId,
    refreshSessionResources,
    resourcePanelActive,
  ]);

  const {
    changesHint: defaultViewChangesHint,
    changesHintLoading: defaultViewChangesLoading,
    openChangesetFileInPreview: openSessionChangeInPreview,
  } = useSessionChangesPreview({
    activeSession,
    activeSessionWorkspaceId,
    contentView: state.contentView,
    activeChangeset: state.activeChangeset,
    sessionChangesLoading: state.sessionChangesLoading,
    sessionChangesError: state.sessionChangesError,
    layoutAuxiliaryVisible: state.uiSettings.layout.auxiliary_visible,
    auxiliaryVisible,
    auxiliaryTab,
    activeTurnTimeline,
    conversationCount: conversations.length,
    activePreviewPath,
    loadSessionChangesets,
    switchContentView,
    setAuxiliaryVisible,
    setStatus,
    openSessionChangePreview: workspacePreview.openSessionChangePreview,
  });
  const handleOpenAttachment = useCallback(
    (sessionId: string, attachment: AttachmentRef) => {
      setSelectedAttachmentPreview({ sessionId, attachment });
      openAuxiliaryTab("files");
    },
    [openAuxiliaryTab],
  );
  const {
    openExtensionWindow,
    openExtensionResource,
    createExtensionReplacement,
    handleExitExtensionWindow,
    extensionWindowVisible,
    extensionDebugSplitActive,
    runtimePreviewTab,
  } = useGatewayExtensionWindow({
    extensionWindowRequested,
    extensionWindowFallback,
    setExtensionWindowFallback,
    auxiliaryTab,
    sharedPreviewVisible,
    activeRuntimePreview,
    sessionResources: state.sessionResources,
    extensionResources,
    openAuxiliaryTab,
    openBrowserPreview: workspacePreview.openBrowserPreview,
    openTerminalPreview: workspacePreview.openTerminalPreview,
    apiPort: resolvedApiPort,
    activeSessionWorkspaceId,
    activeSessionId: activeSession?.session_id ?? null,
    activeMainThread: activeMainResourceThread,
    setStatus,
  });
  const {
    extensionDebugAreaRatios,
    resetExtensionDebugAreaRatios,
    startLayoutResize,
  } = useMainAreaResize({
    mainAreaRatios,
    setMainAreaRatios,
    persistLayoutSettings,
    extensionDebugSplitActive,
  });

  const resetMainAreaRatios = () => {
    if (extensionDebugSplitActive) {
      resetExtensionDebugAreaRatios();
      return;
    }
    const ratios = { ...DEFAULT_MAIN_AREA_RATIOS };
    setMainAreaRatios(ratios);
    persistLayoutSettings({ main_area_ratios: ratios });
  };

  const handleOpenChangesView = () => {
    setAuxiliaryVisible(true);
    setAuxiliaryTab("changes");
    persistLayoutSettings({ auxiliary_visible: true, auxiliary_tab: "changes" });
  };
  const {
    resetBottomPanelHeight,
    startBottomPanelResize,
  } = useBottomPanelResize({
    workspaceId: bottomPanelWorkspaceId,
    panelState: bottomPanelState,
    setWorkspaceBottomPanelStates,
    updateBottomPanelState,
  });
  const {
    nameDialog,
    forkDialog,
    updateForkDialogSelection,
    closeForkDialog,
    submitForkDialog,
    nameDialogSubmitting,
    nameDialogError,
    sessionCatalogRefreshVersions,
    sessionCatalogSyncKeys,
    invalidateSessionCatalog,
    createSessionInCatalog: handleCreateSession,
    createSessionInFolder: handleCreateSessionInFolder,
    createSessionFolder: handleCreateSessionFolder,
    handleSessionFolderDeleted,
    selectAgentSession: handleSelectAgentSession,
    removeWorkspace: handleRemoveWorkspace,
    openRenameDialog: handleRenameSession,
    removeSession: handleDeleteSession,
    changeSessionParent: handleSetSessionParent,
    forkSession: handleForkSessionContext,
    closeNameDialog,
    submitNameDialog,
  } = useSessionCatalogActions({
    apiPort: resolvedApiPort,
    activeGatewayWorkspaceId: state.activeGatewayWorkspaceId,
    activeSessionId: activeSession?.session_id ?? null,
    sessionsByWorkspace: state.sessionsByWorkspace,
    gatewayWorkspaces: state.gatewayWorkspaces,
    confirm,
    setStatus,
    sessionCatalogOutboxRuntime,
    gatewayUserAccess: state.gatewayUserAccess,
    activateGatewayWorkspace,
    createSession,
    openWorkspaceSession,
    removeGatewayWorkspace,
    deleteSession,
    renameSession,
    forkSessionContext,
    readSessionSelectionIntent,
    onForkCreated: async (_sourceWorkspaceId, result, selectionIntent) => {
      const health = await getGatewayHealth(resolvedApiPort);
      const targetWorkspaceId = requireForkTargetWorkspace(
        state.gatewayWorkspaces, health.gateway_id, result,
      ).workspace_id;
      invalidateSessionCatalog(targetWorkspaceId);
      if (selectionIntent !== readSessionSelectionIntent()) return;
      if (result.creation_kind === "new_session_main") {
        await openWorkspaceSession(targetWorkspaceId, result.session.session_id, selectionIntent);
      } else {
        setForkChildOwner({
          workspaceId: targetWorkspaceId, sessionId: result.session.session_id,
          mainThreadId: result.session.thread_id ?? null, initialThreadSelection: result.thread_id,
        });
        openAuxiliaryTab("resources");
      }
    },
    setSessionParent,
  });
  return (
    <WorkspaceFileReferenceProvider
      apiPort={resolvedApiPort}
      workspaceId={activeSessionWorkspaceId}
      onOpen={(content, reference) => {
        openAuxiliaryTab("files");
        workspacePreview.openWorkspaceFileReference(content, reference);
      }}
    >
      <div
      className={`app-shell agent-sessions-workbench shell-gradient-background${extensionWindowVisible ? " extension-window" : ""} ${agentSessionsVisible ? "agent-sessions-open" : "agent-sessions-closed"}`}
      data-agent-sessions-open={String(agentSessionsVisible)}
      data-window-mode={extensionWindowVisible ? "extension" : "standard"}
      data-bt-surface="canvas"
    >
      <Toolbar
        sessionTitle={
          extensionWindowVisible
            ? "扩展窗口"
            : workbenchView === "gateway"
            ? "Gateway 控制台"
            : state.currentSession?.title ?? null
        }
        onCreateSession={() => {
          if (workbenchView === "gateway") {
            handleWorkbenchViewChange("sessions");
          }
          void handleCreateSession().catch((error: unknown) => {
            console.error("在默认 home 工作区创建会话失败", error);
          });
        }}
        auxiliaryVisible={auxiliaryVisible}
        onToggleAuxiliaryPanel={handleToggleAuxiliaryPanel}
        chatVisible={chatVisible}
        onToggleChatPanel={handleToggleChatPanel}
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
          onOpenDiagnostic={openOutputDiagnostic}
          isOpen={agentSessionsVisible && workbenchView === "sessions"}
          workspaceName={state.workspaceName ?? ""}
          gatewayWorkspaces={state.gatewayWorkspaces}
          gatewayWorkspacesStale={state.gatewayWorkspacesStale}
          activeGatewayWorkspaceId={state.activeGatewayWorkspaceId}
          removingGatewayWorkspaceIds={state.removingGatewayWorkspaceIds}
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
          sessionCatalogOutboxRuntime={sessionCatalogOutboxRuntime}
          sessionCatalogOutboxSnapshot={sessionCatalogOutboxSnapshot}
          gatewayUserAccess={state.gatewayUserAccess}
          flexRatio={mainAreaRatios.agent_sessions}
          preferences={agentSessionsPreferences}
          onPreferencesChange={(updater) => {
            persistUiSettings((current) => ({
              session_sidebar: updater(current.session_sidebar),
            }));
          }}
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
          onStart={startManagedGatewayWorkspaceBackend}
          onStop={stopManagedGatewayWorkspaceBackend}
          onSafeRestart={safeRestartManagedGatewayWorkspaceBackend}
          onForceRestart={forceRestartManagedGatewayWorkspaceBackend}
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
          className={`content-layout${auxiliaryVisible ? "" : " auxiliary-collapsed"}${chatVisible ? "" : " chat-collapsed"}`}
        >
          {chatVisible ? (
            <section
              className="chat-panel sessions-part-card"
              data-bt-surface="workspace"
              style={{ flexBasis: 0, flexGrow: mainAreaRatios.chat }}
            >
              <div className="session-view-surface">
                {contentViewSlotsVisible ? (
                  <div className="session-view-content">
                    <ContentViewSlots
                      error={state.error}
                      isBootstrapping={state.isBootstrapping}
                      onRetryGatewayState={refreshGatewayState}
                      contentView={state.contentView}
                      apiPort={resolvedApiPort}
                      workspaceId={activeSessionWorkspaceId}
                      session={activeSession}
                      sessionId={activeSession?.session_id ?? null}
                      hasActiveSession={Boolean(activeSession)}
                      activeSessionCacheKey={activeSessionCacheKey}
                      expandedDetails={state.expandDetails}
                      agentStateJsonl={state.agentStateJsonl}
                      agentStateMessageCount={state.agentStateMessageCount}
                      agentStateLoadedAt={state.agentStateLoadedAt}
                      agentStateLoading={state.agentStateLoading}
                      agentStateError={state.agentStateError}
                      receivedEvents={receivedEvents}
                      activeTraceHistory={activeTraceHistory}
                      onLoadOlderTraceHistory={loadOlderTraceHistory}
                      onRetryTraceHistory={refreshTraceHistory}
                      requestLogs={state.llmRequestLogs}
                      requestLogsLoading={state.llmRequestLogsLoading}
                      requestLogsError={state.llmRequestLogsError}
                      requestLogsLoadedAt={state.llmRequestLogsLoadedAt}
                      onRetryRequestLogs={() => {
                        const retrySessionId = activeSession?.session_id;
                        if (retrySessionId) void refreshLLMRequestLogs(retrySessionId);
                      }}
                      conversations={conversations}
                      activeTurnTimeline={activeTurnTimeline}
                      changesHint={defaultViewChangesHint}
                      changesHintLoading={defaultViewChangesLoading}
                      activeChangeset={state.activeChangeset}
                      gatewayUserViewStates={state.gatewayUserViewStates}
                      onLoadOlderMessages={loadOlderMessages}
                      onLoadNewerMessages={loadNewerMessages}
                      onLoadAroundTurn={loadAroundTurn}
                      onLoadTurnDetails={loadTurnDetails}
                      onLoadToolDetails={loadToolDetails}
                      onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
                      onRetryHistory={refreshTurnHistory}
                      onOpenChanges={handleOpenChangesView}
                      onReplayTurn={replayTurn}
                      onRemovePending={removePendingRequest}
                      onChangePendingPolicy={updatePendingRequestPolicy}
                      onOpenAttachment={handleOpenAttachment}
                      onOpenChildReport={(source) => {
                        if (!activeSessionWorkspaceId || !source.source_session_id || source.source_session_id !== activeSession?.session_id
                          || !source.source_thread_id || !source.source_execution_id) {
                          throw new Error("报告来源不属于当前会话的精确子线程执行");
                        }
                        setForkChildOwner(null);
                        childConversation.selectThreadForOwner(
                          { workspaceId: activeSessionWorkspaceId, sessionId: source.source_session_id },
                          source.source_thread_id, "report", source.source_execution_id,
                        );
                        openAuxiliaryTab("resources");
                      }}
                      onForkConversation={(point) => {
                        void handleForkSessionContext(point.workspaceId, point.sessionId, point);
                      }}
                      onViewStateChange={saveSessionViewState}
                      onViewStateRestoreStatus={setStatus}
                    />
                  </div>
                ) : null}
                {activeSession ? (
                  <>
                    <PendingQueueBar
                      conversations={conversations}
                      onClear={clearPendingRequests}
                      onRemove={removePendingRequest}
                      onChangePolicy={updatePendingRequestPolicy}
                    />
                    <Composer />
                  </>
                ) : null}
              </div>
            </section>
          ) : null}
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
                  tabOrder={extensionWindowRequested
                    ? extensionWindowRequest?.kind === "debug"
                      ? ["debug", "resources"]
                      : ["resources", "debug"]
                    : auxiliaryTabOrder}
                  onSelectAuxiliaryTab={openAuxiliaryTab}
                  onReorderAuxiliaryTabs={handleAuxiliaryTabReorder}
                />
                <div className={`workspace-editor-body workspace-editor-body-${auxiliaryTab}${
                  sharedPreviewVisible ? " has-shared-preview" : ""
                }`}>
                  {sharedPreviewTab ? (
                    sharedPreviewVisible ? (
                      <WorkspaceFilePreviewArea
                        context={auxiliaryTab === "changes" ? "changes" : "files"}
                        visible
                        flexRatio={extensionDebugSplitActive
                          ? extensionDebugAreaRatios.workspace_preview
                          : mainAreaRatios.workspace_preview}
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
                        debugMode={extensionWindowVisible && auxiliaryTab === "debug"}
                        debugExecutionPath={nodeDebugActiveFrame?.path ?? nodeDebugController.state?.script_path ?? null}
                        debugExecutionLine={nodeDebugActiveFrame?.line ?? null}
                        debugBreakpoints={nodeDebugController.state?.breakpoints ?? []}
                        debugActionBusy={nodeDebugController.actionBusy}
                        onChangeDebugBreakpoint={changeNodeDebugBreakpoint}
                      />
                    ) : null
                  ) : null}
                  {auxiliaryLeftVisible ? (
                    <button
                      type="button"
                      className="layout-sash layout-sash-auxiliary-left"
                      title={extensionDebugSplitActive
                        ? "拖拽调整代码预览与调试面板宽度，双击还原"
                        : "拖拽调整代码预览与信息区宽度，双击还原"}
                      aria-label={extensionDebugSplitActive
                        ? "调整代码预览与调试面板宽度"
                        : "调整代码预览与信息区宽度"}
                      onPointerDown={(event) => startLayoutResize("auxiliary-left", event)}
                      onDoubleClick={resetMainAreaRatios}
                    />
                  ) : null}
                  <WorkspaceAuxiliaryPanel
                    visible={auxiliaryVisible}
                    flexRatio={extensionDebugSplitActive
                      ? extensionDebugAreaRatios.auxiliary
                      : sharedPreviewTab && sharedPreviewVisible
                        ? mainAreaRatios.auxiliary
                        : mainAreaRatios.workspace_preview + mainAreaRatios.auxiliary}
                    tab={auxiliaryTab}
                    apiPort={resolvedApiPort}
                    workspaceId={activeSessionWorkspaceId}
                    workspaceFileTreeReady={
                      !state.isBootstrapping
                      && state.gatewayUserAccess !== null
                      && activeSessionWorkspaceId !== null
                    }
                    workspaceName={state.workspaceName ?? ""}
                    sessionId={activeSession?.session_id ?? ""}
                    sessionTitle={activeSession?.title ?? "新会话"}
                    extensionWindow={extensionWindowVisible}
                    activeFilePath={activeFilePath}
                    sessionChangesets={state.sessionChangesets}
                    selectedChangesetId={state.selectedChangesetId}
                    activeChangeset={state.activeChangeset}
                    sessionChangesLoading={state.sessionChangesLoading}
                    sessionChangesError={state.sessionChangesError}
                    sessionChangesLoadedAt={state.sessionChangesLoadedAt}
                    searchOpen={fileTreeSearchOpen}
                    collapseVersion={fileTreeCollapseVersion}
                    settingsVersion={fileTreeSettingsVersion}
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
                    attachmentPreview={
                      selectedAttachmentPreview
                      && selectedAttachmentPreview.sessionId === activeSession?.session_id
                        ? (
                          <WorkspaceAttachmentPreview
                            key={JSON.stringify([activeSessionWorkspaceId, selectedAttachmentPreview.sessionId, selectedAttachmentPreview.attachment.file_id])}
                            onDirectoryShortcutUpdated={() => setFileTreeSettingsVersion((version) => version + 1)}
                            attachment={selectedAttachmentPreview.attachment}
                            apiPort={resolvedApiPort}
                            sessionId={selectedAttachmentPreview.sessionId}
                            workspaceId={activeSessionWorkspaceId}
                          />
                        )
                        : null
                    }
                    goalPanel={extensionWindowRequested ? null : (
                      <GoalManagerCard
                        key={activeSessionCacheKey ?? "no-session"}
                        sessionId={activeSession?.session_id ?? ""}
                        goal={state.currentGoalSessionId === activeSession?.session_id
                          ? state.currentGoal : null}
                        loading={Boolean(activeSession) && (
                          state.goalLoading
                          || state.currentGoalSessionId !== activeSession?.session_id
                        )}
                        error={state.currentGoalSessionId === activeSession?.session_id
                          ? state.goalError : null}
                        onRefresh={() => refreshGoal()}
                        onUpdate={updateGoal}
                        onClear={clearGoal}
                      />
                    )}
                    resourcePanel={(
                      extensionWindowRequested ? (
                        <GatewayExtensionResourcePanel
                          entries={extensionResources.entries}
                          errors={extensionResources.errors}
                          loading={extensionResources.loading}
                          loadedAt={extensionResources.loadedAt}
                          selectedKey={extensionResources.selectedKey}
                          onSelect={extensionResources.select}
                          onRefresh={() => void extensionResources.refresh()}
                          onControl={extensionResources.control}
                          currentOwner={activeSessionWorkspaceId && activeMainResourceThread
                            ? { workspaceId: activeSessionWorkspaceId, thread: activeMainResourceThread } : null}
                          onOwnerControl={async (entry, action, thread) => {
                            if (action === "attach") {
                              if (!activeSession || activeSessionWorkspaceId !== entry.workspace_id
                                || !activeMainResourceThread
                                || activeMainResourceThread.session_id !== activeSession.session_id) {
                                throw new Error("关联资源需要已解析的同工作区主线程");
                              }
                              thread ??= activeMainResourceThread;
                              if (entry.resource.retention_scope === "thread"
                                && entry.resource.source_thread?.session_id !== thread.session_id) {
                                throw new Error("临时资源只能关联来源会话中的线程");
                              }
                            }
                            await extensionResources.ownerControl(entry, action, thread);
                          }}
                          onOpen={openExtensionResource}
                          onCreateReplacement={createExtensionReplacement}
                        />
                      ) : (
                        <>
                          <ResourcePanel
                            resources={state.sessionResources}
                            loading={state.sessionResourcesLoading || state.gatewayUserAccess === null}
                            error={state.sessionResourcesError}
                            loadedAt={state.sessionResourcesLoadedAt}
                            sessionId={activeSession?.session_id ?? ""}
                            workspaceId={activeSessionWorkspaceId}
                            mainThread={activeMainResourceThread}
                            extensionWindow={extensionWindowVisible}
                            activePreviewPath={activeRuntimePreview?.path ?? null}
                            onRefresh={() => {
                              if (activeSession) {
                                void refreshSessionResources(activeSession.session_id).then((result) => {
                                  if (result === "failed") setStatus("后台连接刷新失败，请查看运行与连接中的错误详情");
                                });
                              }
                            }}
                            onControl={controlSessionResource}
                            onOwnerControl={async (resourceId, action, thread) => {
                              if (!activeSession || !activeSessionWorkspaceId) throw new Error("资源管理需要当前会话和工作区");
                              let operationFailed = false;
                              let operationError: unknown;
                              try {
                                const ownerResource = await getWorkspaceResource(resolvedApiPort, activeSessionWorkspaceId, resourceId);
                                const targetThread = action === "attach" ? thread ?? activeMainResourceThread ?? undefined : thread;
                                if (action === "attach" && !targetThread) throw new Error("关联资源需要已解析的当前会话主线程");
                                await controlWorkspaceResource(resolvedApiPort, activeSessionWorkspaceId, ownerResource, action, targetThread);
                                if (ownerResource.kind === "terminal") await workspaceTerminals.refresh();
                              } catch (cause) {
                                operationFailed = true;
                                operationError = cause;
                              }
                              const refresh = await refreshSessionResources(activeSession.session_id);
                              if (operationFailed) {
                                if (refresh === "failed") {
                                  setStatus("资源管理操作失败，且会话资源重读失败；查看运行与连接中的错误详情");
                                }
                                throw operationError;
                              }
                              if (refresh !== "loaded") throw new Error("资源管理操作已完成，但会话资源未能刷新");
                            }}
                            onOpenTerminalPreview={(terminalId) => {
                              openTerminalPanel(terminalId);
                            }}
                            onOpenTerminalExtension={(terminalId) => {
                              openExtensionWindow("terminal", terminalId);
                            }}
                            onOpenBrowserPreview={(browserId) => {
                              openExtensionWindow("browser", browserId);
                            }}
                            onCloseResourcePreview={(kind, resourceId) =>
                              workspacePreview.closeWorkspaceFilePreview(`${kind}://${resourceId}`)
                            }
                            onCreateConnection={async (kind, thread) => {
                              if (!activeSession || !activeSessionWorkspaceId) {
                                throw new Error("新建连接需要当前会话和 Gateway workspace_id");
                              }
                              if (thread.session_id !== activeSession.session_id) {
                                throw new Error("新建连接目标不属于当前主会话");
                              }
                              if (kind === "terminal") {
                                if (bottomPanelWorkspaceId !== activeSessionWorkspaceId) {
                                  throw new Error("终端归属工作区与当前底部面板不一致，无法在这里打开");
                                }
                                await createTerminalFromResourcePanel({
                                  sessionId: activeSession.session_id,
                                  thread,
                                  createTerminal: workspaceTerminals.create,
                                  refreshSessionResources,
                                  closeChildConversation: () => {
                                    closeChildConversationAndClearForkSelection(
                                      childConversation.close,
                                      () => setForkChildOwner((current) => current
                                        ? { ...current, initialThreadSelection: null }
                                        : current),
                                    );
                                  },
                                  openTerminalPanel,
                                });
                                return;
                              }
                              const created = await createSessionConnection(
                                resolvedApiPort,
                                activeSessionWorkspaceId,
                                thread,
                                kind,
                              );
                              const refresh = await refreshSessionResources(activeSession.session_id);
                              if (refresh !== "loaded") {
                                throw new Error(`浏览器 ${created.resourceId} 已创建，但会话连接列表未能刷新`);
                              }
                              openExtensionWindow("browser", created.resourceId);
                            }}
                          />
                          <ChildThreadPanel
                            threads={childThreads.threads}
                            teams={childThreads.teams}
                            total={childThreads.total}
                            loading={childThreads.loading}
                            error={childThreads.error}
                            loadedAt={childThreads.loadedAt}
                            sessionId={childOwnerSessionId ?? ""}
                            ownerLabel={childOwnerLabel}
                            mainThreadId={forkChildOwner ? forkChildOwner.mainThreadId : nodeDebugMainThreadId}
                            activeThreadId={childOwnerWorkspaceId === activeSessionWorkspaceId && childOwnerSessionId === activeSession?.session_id ? nodeDebugThreadId : null}
                            onRefresh={() => {
                              void childThreads.refresh();
                            }}
                            onSelectThread={childOwnerWorkspaceId === activeSessionWorkspaceId && childOwnerSessionId === activeSession?.session_id ? selectDebugThread : undefined}
                            onForkFromThread={(threadId, point) => {
                              if (!childOwnerSessionId || !childOwnerWorkspaceId) return;
                              void handleForkSessionContext(childOwnerWorkspaceId, childOwnerSessionId, { ...point, threadId });
                            }}
                            conversation={childConversation}
                          />
                        </>
                      )
                    )}
                    runtimePreview={runtimePreviewTab ? (
                      <WorkspaceRuntimePreviewArea
                        tab={runtimePreviewTab}
                        onClose={async () => {
                          if (extensionWindowRequested) {
                            extensionResources.select(null);
                            return;
                          }
                          if (runtimePreviewTab) {
                            await workspacePreview.closeWorkspaceFilePreview(runtimePreviewTab.path);
                          }
                        }}
                        extensionWindow={extensionWindowVisible}
                        onExitExtensionWindow={extensionWindowVisible ? handleExitExtensionWindow : undefined}
                      />
                    ) : null}
                    debugPanel={debugPanel}
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
                          { refreshList: true },
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
      <WorkbenchBottomPanel
        visible={panelVisible}
        state={bottomPanelState}
        apiPort={resolvedApiPort}
        workspaceId={bottomPanelWorkspaceId}
        diagnostic={outputDiagnostic?.workspaceId === bottomPanelWorkspaceId
          ? outputDiagnostic.diagnostic : null}
        workspace={bottomPanelWorkspace}
        workspaceName={state.workspaceName ?? ""}
        terminalController={workspaceTerminals}
        terminalThread={terminalResourceTarget.thread}
        terminalThreadTargetUnavailable={terminalResourceTarget.unavailable}
        onUpdateState={updateBottomPanelState}
        onStartResize={startBottomPanelResize}
        onResetHeight={resetBottomPanelHeight}
        onStatusChange={setStatus}
        onOpenSession={handleSelectAgentSession}
      />
        </div>
      </div>
      {!extensionWindowVisible ? (
        <WorkbenchStatusBar
          status={state.status}
          themeBackgroundWarning={state.themeBackgroundWarning}
        />
      ) : null}
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
      <SessionContextForkDialog state={forkDialog} onCancel={closeForkDialog} onSubmit={submitForkDialog} onSelectionChange={updateForkDialogSelection} />
      </div>
    </WorkspaceFileReferenceProvider>
  );
}
