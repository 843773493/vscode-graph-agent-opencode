import AgentStatePanel from "./components/AgentStatePanel";
import BootstrapState from "./components/BootstrapState";
import ChatPanel from "./components/ChatPanel";
import PendingQueueBar from "./components/chat/PendingQueueBar";
import Composer from "./components/composer/Composer";
import EventQueuePanel from "./components/EventQueuePanel";
import AgentSessionsPanel from "./components/AgentSessionsPanel";
import GatewayLogPanel from "./components/GatewayLogPanel";
import AutomationPanel from "./components/AutomationPanel";
import PortForwardPanel from "./components/PortForwardPanel";
import TerminalPanel from "./components/TerminalPanel";
import RequestLogPanel from "./components/RequestLogPanel";
import ResourcePanel from "./components/ResourcePanel";
import GatewayExtensionResourcePanel from "./components/GatewayExtensionResourcePanel";
import SessionNameDialog from "./components/SessionNameDialog";
import { useWarmConfirm } from "./components/WarmConfirmProvider";
import Toolbar, { type WorkbenchView } from "./components/Toolbar";
import GatewayControlCenter from "./components/workspace/GatewayControlCenter";
import WorkspaceEditorHeader from "./components/workspace/WorkspaceEditorHeader";
import WorkspaceFilePreviewArea from "./components/workspace/WorkspaceFilePreviewArea";
import DebugPanel from "./components/workspace/DebugPanel";
import WorkspaceRuntimePreviewArea, {
  type WorkspaceRuntimePreviewTab,
} from "./components/workspace/WorkspaceRuntimePreviewArea";
import { WorkspaceFileReferenceProvider } from "./components/workspace/WorkspaceFileReferenceContext";
import WorkspaceAuxiliaryPanel, {
  type WorkspaceAuxiliaryTab,
} from "./components/workspace/WorkspaceAuxiliaryPanel";
import WorkspaceAttachmentPreview from "./components/workspace/preview/WorkspaceAttachmentPreview";
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
import { useNodeDebugController } from "./hooks/useNodeDebugController";
import { useGatewayExtensionResources } from "./hooks/useGatewayExtensionResources";
import { useSessionGeneratorResources } from "./hooks/sessionResourceExplorer/useSessionGeneratorResources";
import { buildSessionCatalogSyncKeys } from "./hooks/sessionResourceExplorer/resourceTreeSync";
import { createSessionConnection } from "./gatewayApi";
import {
  DEFAULT_GATEWAY_PANEL_HEIGHT,
  DEFAULT_EXTENSION_DEBUG_AREA_RATIOS,
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
import {
  resolveWorkspaceBottomPanelState,
  toWorkspaceBottomPanelSettings,
  type WorkspaceBottomPanelState,
} from "./state/workspaceBottomPanel";
import { resolveAgentSessionsPreferences } from "./state/uiSettings/preferences";
import { buildGatewayAttachUrl } from "./utils/attachUrls";
import type { GatewayExtensionResourceEntry } from "./hooks/useGatewayExtensionResources";
import type {
  AttachmentRef,
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

type ExtensionResourceKind = "browser" | "terminal" | "debug";

type ExtensionWindowRequest = {
  kind: ExtensionResourceKind | null;
  resourceId: string | null;
  workspaceId: string | null;
  sessionId: string | null;
};

const EXTENSION_WINDOW_NAME = "boxteam-extension";
function resolveExtensionWindowRequest(): ExtensionWindowRequest | null {
  if (typeof window === "undefined") {
    return null;
  }
  const params = new URLSearchParams(window.location.search);
  if (window.location.pathname !== "/extension" && params.get("window") !== "extension") {
    return null;
  }
  const browserId = params.get("browserId");
  const resourceId = params.get("resourceId") ?? browserId;
  const resourceType = params.get("resourceType") ?? (browserId ? "browser" : null);
  const kind = resourceType === "browser" || resourceType === "terminal" || resourceType === "debug"
    ? resourceType
    : null;
  return {
    kind,
    resourceId,
    workspaceId: params.get("workspaceId"),
    sessionId: params.get("sessionId"),
  };
}

const DEFAULT_AUXILIARY_TAB_ORDER: WorkspaceAuxiliaryTab[] = [
  "files",
  "changes",
  "debug",
  "resources",
];

type StoredAuxiliaryTab = WorkspaceAuxiliaryTab | "automation";

function resolveAuxiliaryTab(value: StoredAuxiliaryTab | null | undefined): WorkspaceAuxiliaryTab {
  return value === "changes" || value === "resources" || value === "debug"
    ? value
    : "files";
}

function resolveAuxiliaryTabOrder(
  value: ReadonlyArray<StoredAuxiliaryTab> | null | undefined,
): WorkspaceAuxiliaryTab[] {
  const result: WorkspaceAuxiliaryTab[] = [];
  for (const tab of value ?? []) {
    if (tab !== "automation" && !result.includes(tab)) result.push(tab);
  }
  for (const tab of DEFAULT_AUXILIARY_TAB_ORDER) {
    if (!result.includes(tab)) result.push(tab);
  }
  return result;
}

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
    saveSessionViewState,
    setStatus,
    replayTurn,
    updatePendingRequest,
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
  const [nameDialog, setNameDialog] = useState<SessionNameDialogState | null>(null);
  const [sessionCatalogRefreshVersions, setSessionCatalogRefreshVersions] =
    useState<ReadonlyMap<string, number>>(new Map());
  const [workbenchView, setWorkbenchView] = useState<WorkbenchView>(
    () => state.uiSettings.layout.workbench_view ?? "sessions",
  );
  const [nameDialogSubmitting, setNameDialogSubmitting] = useState(false);
  const [nameDialogError, setNameDialogError] = useState<string | null>(null);
  const [auxiliaryTab, setAuxiliaryTab] = useState<WorkspaceAuxiliaryTab>(
    () => extensionWindowRequested
      ? extensionWindowRequest?.kind === "debug" ? "debug" : "resources"
      : resolveAuxiliaryTab(state.uiSettings.layout.auxiliary_tab),
  );
  const [auxiliaryTabOrder, setAuxiliaryTabOrder] = useState<WorkspaceAuxiliaryTab[]>(
    () => resolveAuxiliaryTabOrder(state.uiSettings.layout.auxiliary_tab_order),
  );
  const [auxiliaryVisible, setAuxiliaryVisible] = useState(
    () => extensionWindowRequested
      ? true
      : state.uiSettings.layout.auxiliary_visible ?? defaultAuxiliaryVisible(),
  );
  const [chatVisible, setChatVisible] = useState(
    () => extensionWindowRequested
      ? false
      : state.uiSettings.layout.chat_visible ?? true,
  );
  const [extensionWindowFallback, setExtensionWindowFallback] = useState(false);
  const [workspaceBottomPanelStates, setWorkspaceBottomPanelStates] = useState<
    Record<string, WorkspaceBottomPanelState>
  >({});
  const [fileTreeSearchOpen, setFileTreeSearchOpen] = useState(false);
  const [fileTreeCollapseVersion, setFileTreeCollapseVersion] = useState(0);
  const [markdownSourceVisible, setMarkdownSourceVisible] = useState(false);
  const [mainAreaRatios, setMainAreaRatios] = useState(() =>
    resolveMainAreaRatios(state.uiSettings.layout.main_area_ratios),
  );
  const [extensionDebugAreaRatios, setExtensionDebugAreaRatios] = useState(
    () => ({ ...DEFAULT_EXTENSION_DEBUG_AREA_RATIOS }),
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
  const [selectedAttachmentPreview, setSelectedAttachmentPreview] = useState<{
    sessionId: string;
    attachment: AttachmentRef;
  } | null>(null);
  const lastOpenedChangesPreviewKeyRef = useRef<string | null>(null);
  const defaultViewChangesRequestScopeRef = useRef<string | null>(null);
  const cleanupLayoutResizeRef = useRef<(() => void) | null>(null);
  const activeSession = state.currentSession;
  const activeSessionWorkspaceId =
    state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId;
  useEffect(() => {
    if (
      selectedAttachmentPreview
      && selectedAttachmentPreview.sessionId !== activeSession?.session_id
    ) {
      setSelectedAttachmentPreview(null);
    }
  }, [activeSession?.session_id, selectedAttachmentPreview]);
  const bottomPanelWorkspaceId = state.activeGatewayWorkspaceId ?? activeSessionWorkspaceId;
  const bottomPanelWorkspace = useMemo(
    () => state.gatewayWorkspaces.find(
      (workspace) => workspace.workspace_id === bottomPanelWorkspaceId,
    ) ?? null,
    [bottomPanelWorkspaceId, state.gatewayWorkspaces],
  );
  const bottomPanelState = useMemo(() => {
    const persisted = bottomPanelWorkspaceId
      ? state.uiSettings.layout.bottom_panel_by_workspace?.[bottomPanelWorkspaceId]
      : null;
    return workspaceBottomPanelStates[bottomPanelWorkspaceId ?? ""] ??
      resolveWorkspaceBottomPanelState(persisted, {
        visible: extensionWindowRequested
          ? false
          : state.uiSettings.layout.panel_visible ?? false,
        height: clampGatewayPanelHeight(
          state.uiSettings.layout.panel_height ?? DEFAULT_GATEWAY_PANEL_HEIGHT,
        ),
        tab: state.uiSettings.layout.auxiliary_tab === "automation"
          ? "automation"
          : "output",
        terminalId: null,
      });
  }, [
    bottomPanelWorkspaceId,
    extensionWindowRequested,
    state.uiSettings.layout.bottom_panel_by_workspace,
    state.uiSettings.layout.auxiliary_tab,
    state.uiSettings.layout.panel_height,
    state.uiSettings.layout.panel_visible,
    workspaceBottomPanelStates,
  ]);
  const panelVisible = !extensionWindowRequested && bottomPanelState.visible;
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
    if (extensionWindowRequested) {
      return;
    }
    if (layout.workbench_view) {
      setWorkbenchView(layout.workbench_view);
    }
    if (typeof layout.auxiliary_visible === "boolean") {
      setAuxiliaryVisible(layout.auxiliary_visible);
    }
    if (typeof layout.chat_visible === "boolean") {
      setChatVisible(layout.chat_visible);
    }
    if (layout.auxiliary_tab) {
      setAuxiliaryTab(resolveAuxiliaryTab(layout.auxiliary_tab));
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
  }, [extensionWindowRequest?.kind, extensionWindowRequested, state.uiSettings]);

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
  const updateBottomPanelState = useCallback(
    (patch: Partial<WorkspaceBottomPanelState>) => {
      if (!bottomPanelWorkspaceId) {
        return;
      }
      const nextState: WorkspaceBottomPanelState = {
        ...bottomPanelState,
        ...patch,
      };
      setWorkspaceBottomPanelStates((previous) => ({
        ...previous,
        [bottomPanelWorkspaceId]: nextState,
      }));
      persistUiSettings((current) => ({
        layout: {
          bottom_panel_by_workspace: {
            ...(current.layout.bottom_panel_by_workspace ?? {}),
            [bottomPanelWorkspaceId]: toWorkspaceBottomPanelSettings(nextState),
          },
        },
      }));
    },
    [bottomPanelState, bottomPanelWorkspaceId, persistUiSettings],
  );
  useEffect(() => {
    if (
      extensionWindowRequested ||
      state.uiSettings.layout.auxiliary_tab !== "automation" ||
      !bottomPanelWorkspaceId
    ) {
      return;
    }
    if (bottomPanelState.tab !== "automation") {
      updateBottomPanelState({
        visible: true,
        tab: "automation",
        terminalId: null,
      });
    }
    persistLayoutSettings({ auxiliary_tab: "files" });
  }, [
    bottomPanelState.tab,
    bottomPanelWorkspaceId,
    extensionWindowRequested,
    persistLayoutSettings,
    state.uiSettings.layout.auxiliary_tab,
    updateBottomPanelState,
  ]);
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
  const nodeDebugController = useNodeDebugController({
    apiPort: resolvedApiPort,
    workspaceId: activeSessionWorkspaceId,
    sessionId: activeSession?.session_id ?? null,
    enabled: auxiliaryVisible && auxiliaryTab === "debug",
    onStatusChange: setStatus,
  });
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
      || (panelVisible && bottomPanelState.tab === "terminal"),
  });
  const workspaceTerminalEntries = useMemo(
    () => extensionResources.entries.filter(
      (entry) => entry.workspace_id === bottomPanelWorkspaceId &&
        entry.resource.kind === "terminal" &&
        entry.resource.status === "running",
    ),
    [bottomPanelWorkspaceId, extensionResources.entries],
  );
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
  const resourcePanelActive =
    auxiliaryVisible
    && auxiliaryTab === "resources"
    && state.gatewayUserAccess !== null;

  const sharedPreviewTab = auxiliaryTab === "files" || auxiliaryTab === "changes" || (
    auxiliaryTab === "debug" && (extensionWindowRequested || extensionWindowFallback)
  );
  const sharedPreviewVisible = sharedPreviewTab && (
    codePreviewTabs.length > 0 ||
    codePreviewLoadingPath !== null ||
    codePreviewError !== null
  );
  const auxiliaryLeftVisible = sharedPreviewTab && sharedPreviewVisible;
  const nodeDebugActiveFrame = nodeDebugController.state?.call_stack?.[0] ?? null;
  const debugPanel = (
    <DebugPanel
      apiPort={resolvedApiPort}
      workspaceId={activeSessionWorkspaceId}
      sessionId={activeSession?.session_id ?? null}
      activeFilePath={activeFilePath}
      nodeDebugController={nodeDebugController}
      sessions={sortedSessions}
      compact={!extensionWindowRequested && !extensionWindowFallback}
      onOpenExtensionWindow={() => openExtensionWindow("debug")}
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
    if (!resourcePanelActive || !activeSession) {
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
  }, [
    activeSession,
    activeSessionCacheKey,
    refreshSessionResources,
    resourcePanelActive,
  ]);

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
    const requestScope =
      (activeSessionWorkspaceId ?? "") + ":" + activeSession.session_id;
    if (defaultViewChangesRequestScopeRef.current === requestScope) {
      return;
    }
    defaultViewChangesRequestScopeRef.current = requestScope;
    setDefaultViewChangesLoading(true);
    let requestStarted = false;
    const timerId = window.setTimeout(() => {
      requestStarted = true;
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
          defaultViewChangesRequestScopeRef.current = null;
          const message = error instanceof Error ? error.message : String(error);
          setDefaultViewChangesHint(null);
          setStatus(`会话文件变更提示加载失败: ${message}`);
        })
        .finally(() => {
          if (!cancelled) {
            setDefaultViewChangesLoading(false);
          }
        });
    }, 120);

    return () => {
      cancelled = true;
      window.clearTimeout(timerId);
      if (!requestStarted && defaultViewChangesRequestScopeRef.current === requestScope) {
        defaultViewChangesRequestScopeRef.current = null;
      }
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
  const handleToggleChatPanel = () => {
    const nextVisible = !chatVisible;
    setChatVisible(nextVisible);
    persistLayoutSettings({ chat_visible: nextVisible });
    setStatus(nextVisible ? "会话区已展开" : "会话区已收起");
  };
  const handleTogglePanel = () => {
    const nextVisible = !bottomPanelState.visible;
    updateBottomPanelState({ visible: nextVisible });
    setStatus(nextVisible ? "底部面板已展开" : "底部面板已收起");
  };
  const handleAuxiliaryTabChange = (tab: WorkspaceAuxiliaryTab) => {
    setAuxiliaryTab(tab);
    if (!extensionWindowRequested) {
      persistLayoutSettings({ auxiliary_tab: tab });
    }
  };

  const handleAuxiliaryTabReorder = (tabOrder: WorkspaceAuxiliaryTab[]) => {
    const nextOrder = resolveAuxiliaryTabOrder(tabOrder);
    setAuxiliaryTabOrder(nextOrder);
    if (!extensionWindowRequested) {
      persistLayoutSettings({ auxiliary_tab_order: nextOrder });
    }
  };
  const openAuxiliaryTab = (tab: WorkspaceAuxiliaryTab) => {
    if (tab !== "resources") {
      setExtensionWindowFallback(false);
    }
    setAuxiliaryVisible(true);
    setAuxiliaryTab(tab);
    if (!extensionWindowRequested) {
      persistLayoutSettings({ auxiliary_visible: true, auxiliary_tab: tab });
    }
  };
  const handleOpenAttachment = useCallback(
    (sessionId: string, attachment: AttachmentRef) => {
      setSelectedAttachmentPreview({ sessionId, attachment });
      openAuxiliaryTab("files");
    },
    [openAuxiliaryTab],
  );
  const openTerminalPanel = (terminalId: string) => {
    if (!bottomPanelWorkspaceId) {
      setStatus("打开终端失败：当前没有活动工作区");
      return;
    }
    updateBottomPanelState({
      visible: true,
      tab: "terminal",
      terminalId,
    });
    setStatus(`已在主窗口底部面板打开终端：${terminalId}`);
  };
  const openExtensionWindow = (kind: ExtensionResourceKind, resourceId?: string) => {
    if (extensionWindowRequested) {
      if (kind === "debug") {
        openAuxiliaryTab("debug");
        return;
      }
      const entry = extensionResources.entries.find(
        (candidate) =>
          candidate.resource.kind === kind &&
          candidate.resource.resource_id === resourceId,
      );
      if (entry) {
        extensionResources.select(entry.key);
      }
      return;
    }

    const url = new URL(window.location.href);
    url.pathname = "/extension";
    url.search = "";
    url.hash = "";
    url.searchParams.set("resourceType", kind);
    if (resourceId) url.searchParams.set("resourceId", resourceId);
    if (activeSessionWorkspaceId) {
      url.searchParams.set("workspaceId", activeSessionWorkspaceId);
    }
    if (activeSession?.session_id) {
      url.searchParams.set("sessionId", activeSession.session_id);
    }
    const extensionWindow = window.open(url.toString(), EXTENSION_WINDOW_NAME);
    if (!extensionWindow) {
      setExtensionWindowFallback(true);
      openAuxiliaryTab(kind === "debug" ? "debug" : "resources");
      if (kind === "browser") {
        workspacePreview.openBrowserPreview(resourceId ?? "");
      } else if (kind === "terminal") {
        workspacePreview.openTerminalPreview(resourceId ?? "");
      }
      setStatus("扩展窗口未能打开，已在当前页面切换为扩展窗口模式；请检查浏览器弹窗权限。");
      return;
    }
    extensionWindow.focus();
    setStatus("已打开扩展窗口；后续扩展内容将在此窗口内切换。");
  };
  const openExtensionResource = (entry: GatewayExtensionResourceEntry) => {
    extensionResources.select(entry.key);
    setStatus(
      `已切换到 ${entry.gateway_name} · ${entry.workspace_name} · ${entry.session_title}`,
    );
  };
  const createExtensionReplacement = async (entry: GatewayExtensionResourceEntry) => {
    const created = await createSessionConnection(
      resolvedApiPort,
      entry.workspace_id,
      entry.session_id,
      "browser",
    );
    await extensionResources.refresh();
    setStatus(`已新建浏览器：${created.resourceId}`);
  };
  const selectedExtensionEntry = extensionResources.selectedEntry;
  const extensionPreviewEntry = selectedExtensionEntry &&
    (selectedExtensionEntry.resource.kind === "browser" ||
      selectedExtensionEntry.resource.kind === "terminal")
    ? selectedExtensionEntry
    : null;
  const extensionPreviewTab: WorkspaceRuntimePreviewTab | null = extensionPreviewEntry
    ? extensionPreviewEntry.resource.kind === "browser"
      ? {
          previewType: "browser",
          path: `gateway-resource://${extensionPreviewEntry.key}`,
          name: extensionPreviewEntry.resource.name,
          scopeLabel: `${extensionPreviewEntry.gateway_name} · ${extensionPreviewEntry.workspace_name} · ${extensionPreviewEntry.session_title}`,
          browserId: extensionPreviewEntry.resource.resource_id,
          attachUrl: buildGatewayAttachUrl(
            "browser",
            extensionPreviewEntry.workspace_id,
            extensionPreviewEntry.resource.resource_id,
            true,
          ),
        }
      : {
          previewType: "terminal",
          path: `gateway-resource://${extensionPreviewEntry.key}`,
          name: extensionPreviewEntry.resource.name,
          scopeLabel: `${extensionPreviewEntry.gateway_name} · ${extensionPreviewEntry.workspace_name} · ${extensionPreviewEntry.session_title}`,
          terminalId: extensionPreviewEntry.resource.resource_id,
          attachUrl: buildGatewayAttachUrl(
            "terminal",
            extensionPreviewEntry.workspace_id,
            extensionPreviewEntry.resource.resource_id,
            true,
          ),
        }
    : null;
  const handleExitExtensionWindow = () => {
    if (extensionWindowRequested) {
      if (window.opener && !window.opener.closed) {
        window.close();
        return;
      }
      const standardUrl = new URL(window.location.href);
      standardUrl.pathname = "/";
      standardUrl.search = "";
      standardUrl.hash = "";
      window.location.assign(standardUrl.toString());
      return;
    }
    setExtensionWindowFallback(false);
  };
  const extensionWindowVisible = extensionWindowRequested || extensionWindowFallback;
  const extensionDebugSplitActive = extensionWindowVisible &&
    auxiliaryTab === "debug" &&
    sharedPreviewVisible;
  const activeRuntimePreviewResource = activeRuntimePreview
    ? state.sessionResources.find((resource) =>
        resource.resource_id === (
          activeRuntimePreview.previewType === "browser"
            ? activeRuntimePreview.browserId
            : activeRuntimePreview.terminalId
        )
        && resource.kind === activeRuntimePreview.previewType
        && resource.status === "running",
      ) ?? null
    : null;
  const runtimePreviewTab = extensionWindowRequested
    ? extensionPreviewTab
    : extensionWindowFallback && activeRuntimePreviewResource
      ? activeRuntimePreview
      : null;
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
    const resizingExtensionDebugSplit = target === "auxiliary-left" &&
      extensionDebugSplitActive;
    const startRatios = resizingExtensionDebugSplit
      ? {
          ...mainAreaRatios,
          workspace_preview: extensionDebugAreaRatios.workspace_preview,
          auxiliary: extensionDebugAreaRatios.auxiliary,
        }
      : mainAreaRatios;
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
      if (resizingExtensionDebugSplit) {
        setExtensionDebugAreaRatios({
          workspace_preview: latestRatios.workspace_preview,
          auxiliary: latestRatios.auxiliary,
        });
      } else {
        setMainAreaRatios(latestRatios);
      }
    };

    const finishResize = () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", finishResize);
      window.removeEventListener("pointercancel", finishResize);
      document.body.classList.remove(LAYOUT_RESIZING_CLASS);
      cleanupLayoutResizeRef.current = null;
      if (moved && !resizingExtensionDebugSplit) {
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
    if (extensionDebugSplitActive) {
      setExtensionDebugAreaRatios({ ...DEFAULT_EXTENSION_DEBUG_AREA_RATIOS });
      return;
    }
    const ratios = { ...DEFAULT_MAIN_AREA_RATIOS };
    setMainAreaRatios(ratios);
    persistLayoutSettings({ main_area_ratios: ratios });
  };
  const startGatewayPanelResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    cleanupLayoutResizeRef.current?.();

    const startY = event.clientY;
    const startHeight = bottomPanelState.height;
    let latestHeight = startHeight;
    let moved = false;

    const handlePointerMove = (moveEvent: PointerEvent) => {
      const deltaY = startY - moveEvent.clientY;
      if (deltaY === 0) {
        return;
      }
      moved = true;
      latestHeight = clampGatewayPanelHeight(startHeight + deltaY);
      if (bottomPanelWorkspaceId) {
        setWorkspaceBottomPanelStates((previous) => ({
          ...previous,
          [bottomPanelWorkspaceId]: {
            ...bottomPanelState,
            height: latestHeight,
          },
        }));
      }
    };

    const finishResize = () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", finishResize);
      window.removeEventListener("pointercancel", finishResize);
      document.body.classList.remove(GATEWAY_PANEL_RESIZING_CLASS);
      cleanupLayoutResizeRef.current = null;
      if (moved) {
        updateBottomPanelState({ height: latestHeight });
      }
    };

    document.body.classList.add(GATEWAY_PANEL_RESIZING_CLASS);
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", finishResize);
    window.addEventListener("pointercancel", finishResize);
    cleanupLayoutResizeRef.current = finishResize;
  };
  const resetGatewayPanelHeight = () => {
    updateBottomPanelState({ height: DEFAULT_GATEWAY_PANEL_HEIGHT });
  };
  const handleCreateSession = async (workspaceId?: string | null) => {
    setNameDialog(null);
    setNameDialogError(null);
    // 顶部“新建会话”属于当前 Gateway 工作区；只有尚未激活工作区时
    // 才回退到系统默认 home，避免从测试/远程工作区误建到 home。
    const targetWorkspaceId = workspaceId
      ?? state.activeGatewayWorkspaceId
      ?? state.gatewayWorkspaces.find(
        (workspace) => workspace.system_default,
      )?.workspace_id;
    if (!targetWorkspaceId) {
      const error = new Error("未找到默认 home 工作区，无法创建会话");
      setStatus(`创建会话失败: ${error.message}`);
      throw error;
    }
    try {
      if (targetWorkspaceId !== state.activeGatewayWorkspaceId) {
        await activateGatewayWorkspace(targetWorkspaceId);
      }
      await createSession(DEFAULT_SESSION_TITLE, targetWorkspaceId);
      invalidateSessionCatalog(targetWorkspaceId);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setStatus(`创建会话失败: ${message}`);
      throw error;
    }
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
            hasOlderMessages={activeTurnTimeline?.hasBefore ?? activeTurnTimeline?.hasMore ?? false}
            loadingOlderMessages={activeTurnTimeline?.loadingBefore ?? activeTurnTimeline?.loadingOlder ?? false}
            hasNewerMessages={activeTurnTimeline?.hasAfter ?? false}
            loadingNewerMessages={activeTurnTimeline?.loadingAfter ?? false}
            historyLoading={Boolean(activeSession) && (
              !activeTurnTimeline || activeTurnTimeline.phase === "bootstrapping"
            )}
            projectionState={activeTurnTimeline?.projectionState ?? "ready"}
            timelineGeneration={activeTurnTimeline?.generation ?? 0}
            projectionEpoch={activeTurnTimeline?.projectionEpoch ?? null}
            historyError={activeTurnTimeline?.error ?? null}
            onLoadOlderMessages={loadOlderMessages}
            onLoadNewerMessages={loadNewerMessages}
            onLoadAroundTurn={loadAroundTurn}
            loadingDetailTurnIds={activeTurnTimeline?.loadingDetailIds ?? []}
            onLoadTurnDetails={loadTurnDetails}
            onLoadToolDetails={loadToolDetails}
            onLoadAgentStateMessageRawContent={loadAgentStateMessageRawContent}
            onRetryHistory={refreshTurnHistory}
            sessionChangeSummary={activeSessionChangeHint}
            sessionChangesLoading={defaultViewChangesLoading}
            onOpenChanges={handleOpenChangesView}
            onReplayTurn={replayTurn}
            onUpdatePending={updatePendingRequest}
            onRemovePending={removePendingRequest}
            onChangePendingPolicy={updatePendingRequestPolicy}
            onOpenAttachment={handleOpenAttachment}
            viewState={
              activeSessionCacheKey
                ? state.gatewayUserViewStates.get(activeSessionCacheKey) ?? null
                : null
            }
            onViewStateChange={saveSessionViewState}
            onViewStateRestoreStatus={setStatus}
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
          className={`content-layout${auxiliaryVisible ? "" : " auxiliary-collapsed"}${chatVisible ? "" : " chat-collapsed"}`}
        >
          {chatVisible ? (
            <section
              className="chat-panel sessions-part-card"
              data-bt-surface="workspace"
              style={{ flexBasis: 0, flexGrow: mainAreaRatios.chat }}
            >
              <div className="session-view-surface">
                {activeSession ? (
                  <>
                    <div className="session-view-content">{renderContentView()}</div>
                    <PendingQueueBar
                      conversations={conversations}
                      onClear={clearPendingRequests}
                      onUpdate={updatePendingRequest}
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
                        onChangeDebugBreakpoint={(path, line, breakpointId, definition) => {
                          if (!definition) {
                            if (breakpointId) {
                              void nodeDebugController.runAction(
                                "clear_breakpoint",
                                { breakpoint_id: breakpointId },
                              );
                            }
                            return;
                          }
                          void nodeDebugController.runAction(
                            breakpointId ? "update_breakpoint" : "set_breakpoint",
                            {
                              ...(breakpointId ? { breakpoint_id: breakpointId } : {}),
                              path,
                              line,
                              condition: definition.condition,
                              hit_condition: definition.hit_condition,
                              log_message: definition.log_message,
                            },
                          );
                        }}
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
                    workspaceRoot={state.workspaceRoot ?? ""}
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
                            attachment={selectedAttachmentPreview.attachment}
                            apiPort={resolvedApiPort}
                            sessionId={selectedAttachmentPreview.sessionId}
                            workspaceId={activeSessionWorkspaceId}
                          />
                        )
                        : null
                    }
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
                          onOpen={openExtensionResource}
                          onCreateReplacement={createExtensionReplacement}
                        />
                      ) : (
                        <ResourcePanel
                          resources={state.sessionResources}
                          loading={state.sessionResourcesLoading || state.gatewayUserAccess === null}
                          error={state.sessionResourcesError}
                          loadedAt={state.sessionResourcesLoadedAt}
                          sessionId={activeSession?.session_id ?? ""}
                          workspaceId={activeSessionWorkspaceId}
                          extensionWindow={extensionWindowVisible}
                          activePreviewPath={activeRuntimePreview?.path ?? null}
                          onRefresh={() => {
                            if (activeSession) {
                              void refreshSessionResources(activeSession.session_id);
                            }
                          }}
                          onControl={controlSessionResource}
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
                              openTerminalPanel(created.resourceId);
                            } else {
                              openExtensionWindow("browser", created.resourceId);
                            }
                          }}
                        />
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
            title="拖拽调整底部面板高度，双击还原"
            aria-label="调整底部面板高度"
            onPointerDown={startGatewayPanelResize}
            onDoubleClick={resetGatewayPanelHeight}
          />
          {bottomPanelState.tab === "terminal" ? (
            <TerminalPanel
              entries={workspaceTerminalEntries}
              workspaceId={bottomPanelWorkspaceId}
              workspaceName={state.workspaceName ?? ""}
              selectedTerminalId={bottomPanelState.terminalId}
              height={bottomPanelState.height}
              loading={extensionResources.loading}
              onSelectTerminal={(terminalId) => updateBottomPanelState({
                tab: "terminal",
                terminalId,
              })}
              onRefresh={() => void extensionResources.refresh()}
              onSwitchToOutput={() => updateBottomPanelState({ tab: "output" })}
              onSwitchToPorts={() => updateBottomPanelState({ tab: "ports" })}
              onSwitchToAutomation={() => updateBottomPanelState({ tab: "automation" })}
              onClose={() => updateBottomPanelState({ visible: false })}
            />
          ) : bottomPanelState.tab === "ports" ? (
            <PortForwardPanel
              apiPort={resolvedApiPort}
              workspace={bottomPanelWorkspace}
              height={bottomPanelState.height}
              onSwitchToTerminal={() => updateBottomPanelState({ tab: "terminal" })}
              onSwitchToOutput={() => updateBottomPanelState({ tab: "output" })}
              onSwitchToAutomation={() => updateBottomPanelState({ tab: "automation" })}
              onClose={() => updateBottomPanelState({ visible: false })}
            />
          ) : bottomPanelState.tab === "automation" ? (
            <AutomationPanel
              apiPort={resolvedApiPort}
              generatorResources={generatorResources}
              workspaces={state.gatewayWorkspaces}
              activeWorkspaceId={bottomPanelWorkspaceId}
              currentSessionId={activeSession?.session_id ?? ""}
              workspaceName={bottomPanelWorkspace?.name ?? state.workspaceName ?? ""}
              height={bottomPanelState.height}
              onStatusChange={setStatus}
              onOpenConnectionManager={() => handleWorkbenchViewChange("gateway")}
              onReconnectWorkspace={reconnectGatewayWorkspace}
              onStartWorkspace={startManagedGatewayWorkspaceBackend}
              onSwitchToTerminal={() => updateBottomPanelState({ tab: "terminal" })}
              onSwitchToOutput={() => updateBottomPanelState({ tab: "output" })}
              onSwitchToPorts={() => updateBottomPanelState({ tab: "ports" })}
              onClose={() => updateBottomPanelState({ visible: false })}
            />
          ) : (
            <GatewayLogPanel
              apiPort={resolvedApiPort}
              workspaceId={bottomPanelWorkspaceId}
              height={bottomPanelState.height}
              onOpenTerminal={() => updateBottomPanelState({ tab: "terminal" })}
              onOpenPorts={() => updateBottomPanelState({ tab: "ports" })}
              onOpenAutomation={() => updateBottomPanelState({ tab: "automation" })}
              onClose={() => updateBottomPanelState({ visible: false })}
            />
          )}
        </>
      ) : null}
        </div>
      </div>
      {!extensionWindowVisible ? (
        <footer className="workbench-status-bar" aria-label="状态栏" />
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
      </div>
    </WorkspaceFileReferenceProvider>
  );
}
