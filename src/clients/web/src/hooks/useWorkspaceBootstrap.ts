import { useCallback, useEffect, useRef } from "react";
import {
  DEFAULT_BACKEND_PORT,
  getWorkspace,
  listAgents as apiListAgents,
} from "../api";
import { HttpRequestError } from "../api/http";
import {
  activateGatewayWorkspace,
  ensureGatewayUserAccess,
  getLatestGatewayUserViewState,
  getGatewayUiSettings,
  listGatewayWorkspaces,
} from "../gatewayApi";
import { writeCachedUiSettings } from "../state/storage";
import { sessionScopeKey } from "../state/session/sessionScope";
import type { SetAppState } from "./contentViewLoaderTypes";
import { loadAndApplyResolvedGatewayTheme } from "../theme";
import {
  fetchWorkspaceSessionListSnapshot,
  isCurrentWorkspaceSessionListSnapshot,
  type WorkspaceSessionListSnapshot,
} from "./workspaceSessionListRefresh";

type WorkspaceBootstrapPayload = {
  userAccess: Awaited<ReturnType<typeof ensureGatewayUserAccess>>;
  userViewState: Awaited<ReturnType<typeof getLatestGatewayUserViewState>>;
  gatewayWorkspaces: Awaited<ReturnType<typeof listGatewayWorkspaces>>;
  uiSettings: Awaited<ReturnType<typeof getGatewayUiSettings>>;
  workspace: Awaited<ReturnType<typeof getWorkspace>>;
  workspaceSessionResults: PromiseSettledResult<WorkspaceSessionListSnapshot>[];
  agents: Awaited<ReturnType<typeof apiListAgents>>;
};

const BOOTSTRAP_RETRY_DELAYS_MS = [250, 500, 1000, 2000, 3000, 5000, 5000];

export function isRetryableWorkspaceBootstrapError(error: unknown): boolean {
  return (
    error instanceof HttpRequestError
    && [502, 503, 504].includes(error.status)
  ) || error instanceof TypeError;
}

async function waitForBootstrapRetry(
  delayMs: number,
  signal: AbortSignal,
): Promise<void> {
  await new Promise<void>((resolve) => {
    globalThis.setTimeout(resolve, delayMs);
  });
  signal.throwIfAborted();
}

export function selectBootstrapSessionId({
  preferredSessionId,
  persistedSessionId,
  previousSessionId,
  userChanged,
}: {
  preferredSessionId?: string | null;
  persistedSessionId?: string | null;
  previousSessionId?: string | null;
  userChanged: boolean;
}): string | null {
  if (!userChanged && preferredSessionId) return preferredSessionId;
  if (persistedSessionId) return persistedSessionId;
  return userChanged ? null : (previousSessionId ?? null);
}

export function selectBootstrapToolDetailsExpanded({
  persistedToolDetailsExpanded,
  previousToolDetailsExpanded,
  userChanged,
}: {
  persistedToolDetailsExpanded?: boolean | null;
  previousToolDetailsExpanded: boolean;
  userChanged: boolean;
}): boolean {
  return userChanged
    ? persistedToolDetailsExpanded ?? false
    : previousToolDetailsExpanded;
}

export function canAcceptUserViewStateResponse(
  currentUserId: string | null | undefined,
  responseUserId: string,
): boolean {
  return typeof currentUserId === "string" && currentUserId === responseUserId;
}

export function canAcceptUserViewStateMutation({
  currentUserId,
  responseUserId,
  currentLeaseGeneration,
  requestLeaseGeneration,
}: {
  currentUserId: string | null | undefined;
  responseUserId: string;
  currentLeaseGeneration: number | null | undefined;
  requestLeaseGeneration: number;
}): boolean {
  return (
    canAcceptUserViewStateResponse(currentUserId, responseUserId)
    && currentLeaseGeneration === requestLeaseGeneration
  );
}

export function shouldRestorePersistedWorkspace({
  restorePersistedWorkspace,
  userViewState,
  activeWorkspaceId,
  availableWorkspaceIds,
}: {
  restorePersistedWorkspace: boolean;
  userViewState: Awaited<ReturnType<typeof getLatestGatewayUserViewState>>;
  activeWorkspaceId: string | null;
  availableWorkspaceIds: readonly string[];
}): boolean {
  return (
    restorePersistedWorkspace
    && userViewState !== null
    && availableWorkspaceIds.includes(userViewState.workspace_id)
    && activeWorkspaceId !== userViewState.workspace_id
  );
}

async function loadWorkspaceBootstrap(
  apiPort: number,
  options: {
    restorePersistedWorkspace: boolean;
    checkGatewayWorkspaceHealth: boolean;
    uiSettings?: Awaited<ReturnType<typeof getGatewayUiSettings>>;
    signal?: AbortSignal;
  },
): Promise<WorkspaceBootstrapPayload> {
  const { signal } = options;
  const userAccess = await ensureGatewayUserAccess(apiPort);
  signal?.throwIfAborted();
  const uiSettingsPromise = options.uiSettings
    ? Promise.resolve(options.uiSettings)
    : getGatewayUiSettings(apiPort);
  const gatewayWorkspacesPromise = listGatewayWorkspaces(apiPort, {
    checkHealth: options.checkGatewayWorkspaceHealth,
  });
  const [uiSettings, initialGatewayWorkspaces] = await Promise.all([
    uiSettingsPromise,
    gatewayWorkspacesPromise,
  ]);
  signal?.throwIfAborted();
  const userViewStatePromise = userAccess.kind === "user"
    ? getLatestGatewayUserViewState(apiPort)
    : Promise.resolve(null);
  const userViewState = await userViewStatePromise;
  signal?.throwIfAborted();
  if (!uiSettings.theme.resolved_theme) {
    throw new Error("Gateway UI Settings 缺少已解析主题");
  }
  if (!options.uiSettings) {
    await loadAndApplyResolvedGatewayTheme(uiSettings.theme.resolved_theme);
  }
  let gatewayWorkspaces = initialGatewayWorkspaces;
  const persistedWorkspaceId = userViewState?.workspace_id;
  if (
    shouldRestorePersistedWorkspace({
      restorePersistedWorkspace: options.restorePersistedWorkspace,
      userViewState,
      activeWorkspaceId: gatewayWorkspaces.active_workspace_id,
      availableWorkspaceIds: gatewayWorkspaces.items.map(
        (workspace) => workspace.workspace_id,
      ),
    })
    && persistedWorkspaceId
  ) {
    await activateGatewayWorkspace(apiPort, persistedWorkspaceId, signal);
    signal?.throwIfAborted();
    gatewayWorkspaces = await listGatewayWorkspaces(apiPort, {
      checkHealth: options.checkGatewayWorkspaceHealth,
    });
  }
  signal?.throwIfAborted();
  const activeWorkspaceId = gatewayWorkspaces.active_workspace_id;
  const [workspace, workspaceSessionResults, agents] = await Promise.all([
    getWorkspace(apiPort, activeWorkspaceId),
    Promise.allSettled(
      activeWorkspaceId
        ? [fetchWorkspaceSessionListSnapshot(apiPort, activeWorkspaceId)]
        : [],
    ),
    apiListAgents(apiPort, activeWorkspaceId),
  ]);
  return {
    userAccess,
    userViewState,
    gatewayWorkspaces,
    uiSettings,
    workspace,
    workspaceSessionResults,
    agents,
  };
}

async function loadWorkspaceBootstrapWithRetry(
  apiPort: number,
  options: Parameters<typeof loadWorkspaceBootstrap>[1],
): Promise<WorkspaceBootstrapPayload> {
  for (let attempt = 0; ; attempt += 1) {
    options.signal?.throwIfAborted();
    try {
      return await loadWorkspaceBootstrap(apiPort, options);
    } catch (error: unknown) {
      if (
        !isRetryableWorkspaceBootstrapError(error)
        || attempt >= BOOTSTRAP_RETRY_DELAYS_MS.length
      ) {
        throw error;
      }
      await waitForBootstrapRetry(
        BOOTSTRAP_RETRY_DELAYS_MS[attempt],
        options.signal ?? new AbortController().signal,
      );
    }
  }
}

export function useWorkspaceBootstrap({
  apiPort,
  uiSettings,
  setState,
}: {
  apiPort: number | null;
  uiSettings: Awaited<ReturnType<typeof getGatewayUiSettings>>;
  setState: SetAppState;
}) {
  const refreshGenerationRef = useRef(0);
  const workspaceStatusRefreshGenerationRef = useRef(0);
  const refreshAbortRef = useRef<AbortController | null>(null);
  const currentUiSettingsRef = useRef(uiSettings);
  currentUiSettingsRef.current = uiSettings;

  const invalidateWorkspaceRefreshes = useCallback(() => {
    refreshGenerationRef.current += 1;
    workspaceStatusRefreshGenerationRef.current += 1;
    refreshAbortRef.current?.abort();
    refreshAbortRef.current = null;
  }, []);

  const refreshSessions = useCallback(async (
    preferredSessionId?: string | null,
    options: {
      restorePersistedWorkspace?: boolean;
      checkGatewayWorkspaceHealth?: boolean;
      reuseCurrentUiSettings?: boolean;
    } = {},
  ) => {
    const refreshGeneration = ++refreshGenerationRef.current;
    refreshAbortRef.current?.abort();
    const controller = new AbortController();
    refreshAbortRef.current = controller;
    try {
      const resolvedApiPort = apiPort ?? DEFAULT_BACKEND_PORT;
      const {
        userAccess,
        userViewState,
        gatewayWorkspaces,
        uiSettings,
        workspace,
        workspaceSessionResults,
        agents,
      } = await loadWorkspaceBootstrapWithRetry(resolvedApiPort, {
        restorePersistedWorkspace: options.restorePersistedWorkspace ?? false,
        checkGatewayWorkspaceHealth: options.checkGatewayWorkspaceHealth ?? true,
        uiSettings: options.reuseCurrentUiSettings
          ? currentUiSettingsRef.current
          : undefined,
        signal: controller.signal,
      });
      writeCachedUiSettings(uiSettings);
      const activeWorkspaceId = gatewayWorkspaces.active_workspace_id;
      const workspaceIds = activeWorkspaceId ? [activeWorkspaceId] : [];
      const workspaceSessionEntries: WorkspaceSessionListSnapshot[] = [];
      const workspaceSessionErrors = new Map<string, string>();
      for (const [index, result] of workspaceSessionResults.entries()) {
        if (result.status === "fulfilled") {
          workspaceSessionEntries.push(result.value);
          continue;
        }
        const workspaceId = workspaceIds[index];
        if (!workspaceId) continue;
        const message =
          result.reason instanceof Error
            ? result.reason.message
            : String(result.reason);
        workspaceSessionErrors.set(workspaceId, message);
      }
      // 会话目录异常只代表该分支暂时不可读，不能把健康的 Gateway 工作区
      // 标成 offline，更不能用空数组覆盖之前已经加载的会话。
      const visibleGatewayWorkspaces = gatewayWorkspaces.items;
      const failedWorkspaceNames = visibleGatewayWorkspaces
        .filter((workspace) => workspaceSessionErrors.has(workspace.workspace_id))
        .map((workspace) => workspace.name);
      const partialGatewayError =
        failedWorkspaceNames.length > 0
          ? `部分工作区会话暂不可用，已保留现有会话状态：${failedWorkspaceNames.join("、")}`
          : null;
      if (
        controller.signal.aborted
        || refreshGeneration !== refreshGenerationRef.current
      ) {
        return false;
      }
      setState((prev) => {
        const sessionsByWorkspace = new Map(prev.sessionsByWorkspace);
        const userChanged =
          prev.gatewayUserAccess?.kind !== userAccess.kind
          || prev.gatewayUserAccess?.user_id !== userAccess.user_id;
        const gatewayUserViewStates = userChanged
          ? new Map()
          : new Map(prev.gatewayUserViewStates);
        const turnTimelinesBySession = userChanged
          ? new Map()
          : prev.turnTimelinesBySession;
        const unreadSessionKeys = userChanged
          ? new Set<string>()
          : prev.unreadSessionKeys;
        if (userViewState) {
          gatewayUserViewStates.set(
            sessionScopeKey(userViewState.workspace_id, userViewState.session_id),
            userViewState,
          );
        }
        for (const snapshot of workspaceSessionEntries) {
          if (isCurrentWorkspaceSessionListSnapshot(snapshot)) {
            sessionsByWorkspace.set(snapshot.workspaceId, snapshot.sessions);
          }
        }
        const sessionGatewayWorkspaceById = new Map<string, string>();
        for (const [workspaceId, sessions] of sessionsByWorkspace) {
          for (const session of sessions) {
            sessionGatewayWorkspaceById.set(
              sessionScopeKey(workspaceId, session.session_id),
              workspaceId,
            );
          }
        }
        const activeSessions = activeWorkspaceId
          ? sessionsByWorkspace.get(activeWorkspaceId) ?? []
          : [];
        const targetSessionId = selectBootstrapSessionId({
          preferredSessionId,
          persistedSessionId: userViewState?.session_id,
          previousSessionId: prev.currentSession?.session_id,
          userChanged,
        });
        const nextCurrentSession =
          activeSessions.find(
            (session) => session.session_id === targetSessionId,
          ) ??
          activeSessions[0] ??
          null;
        const sessionChanged =
          (nextCurrentSession?.session_id ?? null) !==
          (prev.currentSession?.session_id ?? null);
        const workspaceChanged =
          activeWorkspaceId !== prev.activeGatewayWorkspaceId ||
          activeWorkspaceId !== prev.currentSessionWorkspaceId;
        const contentTargetChanged = sessionChanged || workspaceChanged;
        return {
          ...prev,
          gatewayWorkspaces: visibleGatewayWorkspaces,
          activeGatewayWorkspaceId: activeWorkspaceId,
          sessionsByWorkspace,
          sessionGatewayWorkspaceById,
          gatewayError: partialGatewayError,
          gatewayUserAccess: userAccess,
          gatewayUserViewStates,
          turnTimelinesBySession,
          // 用户切换会清空旧用户的 Turn 缓存。即使服务端为两个用户
          // 选择了同一个 session_id，也必须让历史 bootstrap effect 重新执行，
          // 否则当前会话会永远停留在“正在加载最新 Turn”。
          sessionHistoryReloadNonce: userChanged
            ? prev.sessionHistoryReloadNonce + 1
            : prev.sessionHistoryReloadNonce,
          unreadSessionKeys,
          uiSettings,
          uiSettingsLoaded: true,
          expandDetails: selectBootstrapToolDetailsExpanded({
            persistedToolDetailsExpanded: userViewState?.tool_details_expanded,
            previousToolDetailsExpanded: prev.expandDetails,
            userChanged,
          }),
          workspaceRoot: workspace.root_path,
          workspaceName: workspace.name,
          agents,
          sessions: activeSessions,
          currentSession: nextCurrentSession,
          currentSessionWorkspaceId: nextCurrentSession ? activeWorkspaceId : null,
          traceEvents: contentTargetChanged ? [] : prev.traceEvents,
          llmRequestLogs: contentTargetChanged ? [] : prev.llmRequestLogs,
          llmRequestLogsLoadedAt: contentTargetChanged ? null : prev.llmRequestLogsLoadedAt,
          sessionResources: contentTargetChanged ? [] : prev.sessionResources,
          sessionResourcesLoadedAt: contentTargetChanged ? null : prev.sessionResourcesLoadedAt,
          agentStateJsonl: contentTargetChanged ? "" : prev.agentStateJsonl,
          agentStateMessageCount: contentTargetChanged ? 0 : prev.agentStateMessageCount,
          agentStateLoadedAt: contentTargetChanged ? null : prev.agentStateLoadedAt,
          error: null,
          agentSessionsPanelOpen:
            uiSettings.layout.agent_sessions_panel_open ?? true,
          contentView: uiSettings.layout.content_view ?? prev.contentView,
          isBootstrapping: false,
        };
      });
      return true;
    } catch (error) {
      if (
        controller.signal.aborted
        || refreshGeneration !== refreshGenerationRef.current
      ) {
        return false;
      }
      const message = error instanceof Error ? error.message : String(error);
      setState((prev) => ({
        ...prev,
        error: message,
        status: "初始化失败",
        isBootstrapping: false,
      }));
      throw error;
    } finally {
      if (refreshAbortRef.current === controller) {
        refreshAbortRef.current = null;
      }
    }
  }, [apiPort, setState]);

  const refreshGatewayWorkspaceStatuses = useCallback(async (
    expectedWorkspaceId?: string | null,
  ): Promise<void> => {
    const requestGeneration = ++workspaceStatusRefreshGenerationRef.current;
    try {
      const workspaceList = await listGatewayWorkspaces(
        apiPort ?? DEFAULT_BACKEND_PORT,
      );
      if (requestGeneration !== workspaceStatusRefreshGenerationRef.current) {
        return;
      }
      setState((previous) => {
        if (
          expectedWorkspaceId
          && (
            previous.activeGatewayWorkspaceId !== expectedWorkspaceId
            || workspaceList.active_workspace_id !== expectedWorkspaceId
          )
        ) {
          return previous;
        }
        return {
          ...previous,
          gatewayWorkspaces: workspaceList.items,
        };
      });
    } catch (error) {
      if (requestGeneration !== workspaceStatusRefreshGenerationRef.current) {
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      setState((previous) => {
        if (
          expectedWorkspaceId
          && previous.activeGatewayWorkspaceId !== expectedWorkspaceId
        ) {
          return previous;
        }
        return {
          ...previous,
          gatewayError: `后台刷新工作区状态失败: ${message}`,
          status: `后台刷新工作区状态失败: ${message}`,
        };
      });
    }
  }, [apiPort, setState]);

  useEffect(() => {
    void refreshSessions(undefined, {
      restorePersistedWorkspace: true,
      checkGatewayWorkspaceHealth: true,
    }).catch(() => {
      // 错误详情已经写入全局状态；这里只处理 effect Promise，避免未处理拒绝。
    });
  }, [refreshSessions]);

  return {
    invalidateWorkspaceRefreshes,
    refreshGatewayWorkspaceStatuses,
    refreshSessions,
  };
}
