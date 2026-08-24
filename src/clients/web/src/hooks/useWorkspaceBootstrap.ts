import { useCallback, useEffect, useRef } from "react";
import {
  DEFAULT_BACKEND_PORT,
  getWorkspace,
  listAgents as apiListAgents,
} from "../api";
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

const initialBootstrapRequests = new Map<
  number,
  Promise<WorkspaceBootstrapPayload>
>();

async function loadWorkspaceBootstrap(
  apiPort: number,
): Promise<WorkspaceBootstrapPayload> {
  const userAccess = await ensureGatewayUserAccess(apiPort);
  const userViewState = userAccess.kind === "user"
    ? await getLatestGatewayUserViewState(apiPort)
    : null;
  const uiSettings = await getGatewayUiSettings(apiPort);
  if (!uiSettings.theme.resolved_theme) {
    throw new Error("Gateway UI Settings 缺少已解析主题");
  }
  await loadAndApplyResolvedGatewayTheme(uiSettings.theme.resolved_theme);
  let gatewayWorkspaces = await listGatewayWorkspaces(apiPort);
  if (
    userViewState &&
    gatewayWorkspaces.items.some(
      (workspace) => workspace.workspace_id === userViewState.workspace_id,
    ) &&
    gatewayWorkspaces.active_workspace_id !== userViewState.workspace_id
  ) {
    await activateGatewayWorkspace(apiPort, userViewState.workspace_id);
    gatewayWorkspaces = await listGatewayWorkspaces(apiPort);
  }
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

function loadInitialWorkspaceBootstrap(apiPort: number) {
  const existing = initialBootstrapRequests.get(apiPort);
  if (existing) {
    return existing;
  }
  const request = loadWorkspaceBootstrap(apiPort).finally(() => {
    if (initialBootstrapRequests.get(apiPort) === request) {
      initialBootstrapRequests.delete(apiPort);
    }
  });
  initialBootstrapRequests.set(apiPort, request);
  return request;
}

export function useWorkspaceBootstrap({
  apiPort,
  setState,
}: {
  apiPort: number | null;
  setState: SetAppState;
}) {
  const refreshGenerationRef = useRef(0);

  const invalidateWorkspaceRefreshes = useCallback(() => {
    refreshGenerationRef.current += 1;
  }, []);

  const refreshSessions = useCallback(async (
    preferredSessionId?: string | null,
    options: { reuseInitialRequest?: boolean } = {},
  ) => {
    const refreshGeneration = ++refreshGenerationRef.current;
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
      } = options.reuseInitialRequest
        ? await loadInitialWorkspaceBootstrap(resolvedApiPort)
        : await loadWorkspaceBootstrap(resolvedApiPort);
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
        const message =
          result.reason instanceof Error
            ? result.reason.message
            : String(result.reason);
        workspaceSessionErrors.set(workspaceId, message);
      }
      if (activeWorkspaceId && workspaceSessionErrors.has(activeWorkspaceId)) {
        throw new Error(
          `当前工作区会话加载失败：${workspaceSessionErrors.get(activeWorkspaceId)}`,
        );
      }
      const visibleGatewayWorkspaces = gatewayWorkspaces.items.map((workspace) =>
        workspaceSessionErrors.has(workspace.workspace_id)
          ? { ...workspace, status: "offline" as const }
          : workspace,
      );
      const failedWorkspaceNames = visibleGatewayWorkspaces
        .filter((workspace) => workspaceSessionErrors.has(workspace.workspace_id))
        .map((workspace) => workspace.name);
      const partialGatewayError =
        failedWorkspaceNames.length > 0
          ? `部分工作区离线，未加载会话：${failedWorkspaceNames.join("、")}`
          : null;
      if (refreshGeneration !== refreshGenerationRef.current) {
        return false;
      }
      setState((prev) => {
        const sessionsByWorkspace = new Map(prev.sessionsByWorkspace);
        const userChanged =
          prev.gatewayUserAccess?.kind !== userAccess.kind
          || prev.gatewayUserAccess?.user_id !== userAccess.user_id
          || (
            userAccess.kind === "guest"
            && prev.gatewayUserAccess?.expires_at !== userAccess.expires_at
          );
        const gatewayUserViewStates = userChanged
          ? new Map()
          : new Map(prev.gatewayUserViewStates);
        const turnTimelinesBySession = userChanged
          ? new Map()
          : prev.turnTimelinesBySession;
        const unreadSessionKeys = userChanged
          ? new Set()
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
      if (refreshGeneration !== refreshGenerationRef.current) {
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
    }
  }, [apiPort, setState]);

  useEffect(() => {
    void refreshSessions(undefined, { reuseInitialRequest: true }).catch(() => {
      // 错误详情已经写入全局状态；这里只处理 effect Promise，避免未处理拒绝。
    });
  }, [refreshSessions]);

  return { invalidateWorkspaceRefreshes, refreshSessions };
}
