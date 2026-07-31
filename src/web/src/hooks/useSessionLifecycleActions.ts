import { useCallback } from "react";
import {
  createSession as apiCreateSession,
  DEFAULT_SESSION_TITLE,
  deleteSession as apiDeleteSession,
  forkSessionContext as apiForkSessionContext,
  getSession as apiGetSession,
  listAgents as apiListAgents,
  listSessions as apiListSessions,
  moveSessionParent as apiMoveSessionParent,
  setWorkspaceDefaultAgent as apiSetWorkspaceDefaultAgent,
  setWorkspaceDefaultProvider as apiSetWorkspaceDefaultProvider,
  updateSession as apiUpdateSession,
  updateSessionAgent as apiUpdateSessionAgent,
  updateSessionProvider as apiUpdateSessionProvider,
} from "../api";
import type { Session } from "../types/backend";
import { cloneMaps } from "../state/appStateMaps";
import {
  clearLastSessionId,
  writeLastSessionId,
} from "../state/storage";
import { replaceSessionMetadata } from "../state/session/sessions";
import { appendFrontendEvent } from "../state/traceEvents";
import { resetAgentStateFields } from "./useAgentStateSnapshot";
import type { SetAppState } from "./contentViewLoaderTypes";
import { sessionScopeKey } from "../state/session/sessionScope";

function normalizeSessionTitle(title: string): string {
  const trimmed = title.trim();
  if (!trimmed) {
    throw new Error("会话名称不能为空");
  }
  return trimmed;
}

export function useSessionLifecycleActions({
  apiPort,
  currentSession,
  activeGatewayWorkspaceId,
  currentSessionGatewayWorkspaceId,
  currentSessionCacheKey,
  defaultGatewayWorkspaceId,
  setState,
  abortCurrentStream,
  invalidateAgentState,
}: {
  apiPort: number;
  currentSession: Session | null;
  activeGatewayWorkspaceId: string | null;
  currentSessionGatewayWorkspaceId: string | null;
  currentSessionCacheKey: string | null;
  defaultGatewayWorkspaceId: string | null;
  setState: SetAppState;
  abortCurrentStream: () => void;
  invalidateAgentState: () => void;
}) {
  const selectSession = useCallback(
    (sessionId: string) => {
      abortCurrentStream();
      invalidateAgentState();
      setState((prev) => {
        const next = cloneMaps(prev);
        const selected =
          prev.sessions.find((session) => session.session_id === sessionId) ??
          prev.currentSession;
        next.currentSession = selected;
        const workspaceId = selected ? prev.activeGatewayWorkspaceId : null;
        next.currentSessionWorkspaceId = workspaceId;
        const cacheKey =
          selected && workspaceId
            ? sessionScopeKey(workspaceId, selected.session_id)
            : sessionId;
        if (selected && workspaceId) {
          next.sessionGatewayWorkspaceById.set(
            cacheKey,
            workspaceId,
          );
        }
        next.unreadSessionKeys.delete(cacheKey);
        next.traceEvents = [];
        next.llmRequestLogs = [];
        next.llmRequestLogsLoadedAt = null;
        next.llmRequestLogsLoading = prev.contentView === "requests";
        next.llmRequestLogsError = null;
        next.sessionResources = [];
        next.sessionResourcesLoadedAt = null;
        next.sessionResourcesLoading = prev.contentView === "resources";
        next.sessionResourcesError = null;
        next.pendingConversations.delete(cacheKey);
        next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
        next.status = "正在加载会话历史";
        next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
        Object.assign(next, resetAgentStateFields(next));
        if (selected) {
          writeLastSessionId(selected.session_id);
          appendFrontendEvent(
            next.eventQueuesBySession,
            selected.session_id,
            "session_selected",
            "切换会话",
            {
              session_id: selected.session_id,
              title: selected.title,
            },
            selected.title,
            cacheKey,
          );
        }
        return next;
      });
    },
    [abortCurrentStream, invalidateAgentState, setState],
  );

  const selectWorkspaceSession = useCallback(
    (workspaceId: string, sessionId: string) => {
      abortCurrentStream();
      invalidateAgentState();
      setState((prev) => {
        const workspaceSessions = prev.sessionsByWorkspace.get(workspaceId) ?? [];
        const selected = workspaceSessions.find(
          (session) => session.session_id === sessionId,
        );
        if (!selected) {
          return {
            ...prev,
            status: `切换会话失败: 工作区 ${workspaceId} 中不存在会话 ${sessionId}`,
          };
        }

        const workspace = prev.gatewayWorkspaces.find(
          (item) => item.workspace_id === workspaceId,
        );
        const next = cloneMaps(prev);
        next.activeGatewayWorkspaceId = workspaceId;
        next.workspaceRoot = workspace?.root_path ?? prev.workspaceRoot;
        next.workspaceName = workspace?.name ?? prev.workspaceName;
        next.sessions = workspaceSessions;
        next.currentSession = selected;
        next.currentSessionWorkspaceId = workspaceId;
        const cacheKey = sessionScopeKey(workspaceId, selected.session_id);
        next.sessionGatewayWorkspaceById.set(cacheKey, workspaceId);
        next.unreadSessionKeys.delete(cacheKey);
        next.traceEvents = [];
        next.llmRequestLogs = [];
        next.llmRequestLogsLoadedAt = null;
        next.llmRequestLogsLoading = prev.contentView === "requests";
        next.llmRequestLogsError = null;
        next.sessionResources = [];
        next.sessionResourcesLoadedAt = null;
        next.sessionResourcesLoading = prev.contentView === "resources";
        next.sessionResourcesError = null;
        next.pendingConversations.delete(cacheKey);
        next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
        next.status = "正在加载会话历史";
        next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
        Object.assign(next, resetAgentStateFields(next));
        writeLastSessionId(selected.session_id);
        appendFrontendEvent(
          next.eventQueuesBySession,
          selected.session_id,
          "session_selected",
          "切换会话",
          {
            session_id: selected.session_id,
            title: selected.title,
            workspace_id: workspaceId,
          },
          selected.title,
          cacheKey,
        );
        return next;
      });
    },
    [abortCurrentStream, invalidateAgentState, setState],
  );

  const createSession = useCallback(
    async (
      title: string = DEFAULT_SESSION_TITLE,
      workspaceId?: string | null,
      folderId?: string | null,
    ) => {
      invalidateAgentState();
      const normalizedTitle = normalizeSessionTitle(title);
      const targetWorkspaceId =
        workspaceId ?? activeGatewayWorkspaceId ?? defaultGatewayWorkspaceId;
      try {
        const session = await apiCreateSession(
          apiPort,
          normalizedTitle,
          targetWorkspaceId,
          folderId,
        );
        setState((prev) => {
          const next = cloneMaps(prev);
          const resolvedWorkspaceId =
            targetWorkspaceId ?? prev.activeGatewayWorkspaceId;
          const workspace = prev.gatewayWorkspaces.find(
            (item) => item.workspace_id === resolvedWorkspaceId,
          );
          next.activeGatewayWorkspaceId = resolvedWorkspaceId;
          next.currentSessionWorkspaceId = resolvedWorkspaceId ?? null;
          next.workspaceRoot = workspace?.root_path ?? prev.workspaceRoot;
          next.workspaceName = workspace?.name ?? prev.workspaceName;
          const previousWorkspaceSessions = resolvedWorkspaceId
            ? prev.sessionsByWorkspace.get(resolvedWorkspaceId) ?? []
            : prev.sessions;
          next.sessions = [
            session,
            ...previousWorkspaceSessions.filter(
              (item) => item.session_id !== session.session_id,
            ),
          ];
          if (resolvedWorkspaceId) {
            next.sessionsByWorkspace.set(resolvedWorkspaceId, next.sessions);
            next.sessionGatewayWorkspaceById.set(
              sessionScopeKey(resolvedWorkspaceId, session.session_id),
              resolvedWorkspaceId,
            );
          }
          next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
          next.currentSession = session;
          writeLastSessionId(session.session_id);
          next.traceEvents = [];
          next.llmRequestLogs = [];
          next.llmRequestLogsLoadedAt = null;
          next.llmRequestLogsLoading = false;
          next.llmRequestLogsError = null;
          next.sessionResources = [];
          next.sessionResourcesLoadedAt = null;
          next.sessionResourcesLoading = false;
          next.sessionResourcesError = null;
          next.status = "已创建会话";
          next.contentView = "default";
          Object.assign(next, resetAgentStateFields(next));
          appendFrontendEvent(
            next.eventQueuesBySession,
            session.session_id,
            "session_created",
            "创建会话",
            {
              session_id: session.session_id,
              title: session.title,
            },
            session.title,
            resolvedWorkspaceId
              ? sessionScopeKey(resolvedWorkspaceId, session.session_id)
              : session.session_id,
          );
          return next;
        });
        return session;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `创建会话失败: ${message}` }));
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      defaultGatewayWorkspaceId,
      invalidateAgentState,
      setState,
    ],
  );

  const startNewSessionDraft = useCallback((workspaceId?: string | null) => {
    abortCurrentStream();
    invalidateAgentState();
    clearLastSessionId();
    setState((prev) => {
      const next = cloneMaps(prev);
      const targetWorkspaceId =
        workspaceId ?? prev.activeGatewayWorkspaceId ?? defaultGatewayWorkspaceId;
      const targetWorkspace = prev.gatewayWorkspaces.find(
        (item) => item.workspace_id === targetWorkspaceId,
      );
      if (targetWorkspaceId && targetWorkspace) {
        next.activeGatewayWorkspaceId = targetWorkspaceId;
        next.workspaceRoot = targetWorkspace.root_path;
        next.workspaceName = targetWorkspace.name;
        next.sessions = prev.sessionsByWorkspace.get(targetWorkspaceId) ?? [];
      }
      next.currentSession = null;
      next.currentSessionWorkspaceId = null;
      next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
      next.traceEvents = [];
      next.llmRequestLogs = [];
      next.llmRequestLogsLoadedAt = null;
      next.llmRequestLogsLoading = false;
      next.llmRequestLogsError = null;
      next.sessionResources = [];
      next.sessionResourcesLoadedAt = null;
      next.sessionResourcesLoading = false;
      next.sessionResourcesError = null;
      next.contentView = "default";
      next.status = "新会话";
      Object.assign(next, resetAgentStateFields(next));
      return next;
    });
  }, [
    abortCurrentStream,
    defaultGatewayWorkspaceId,
    invalidateAgentState,
    setState,
  ]);

  const forkSessionContext = useCallback(
    async (workspaceId: string, sourceSessionId: string) => {
      setState((prev) => ({
        ...prev,
        status: "正在复制 Agent 上下文并创建子会话",
      }));

      try {
        const childSession = await apiForkSessionContext(
          apiPort,
          sourceSessionId,
          workspaceId,
        );
        abortCurrentStream();
        invalidateAgentState();
        setState((prev) => {
          const next = cloneMaps(prev);
          const workspace = prev.gatewayWorkspaces.find(
            (item) => item.workspace_id === workspaceId,
          );
          const workspaceSessions = [
            childSession,
            ...(prev.sessionsByWorkspace.get(workspaceId) ?? []).filter(
              (item) => item.session_id !== childSession.session_id,
            ),
          ];
          const cacheKey = sessionScopeKey(
            workspaceId,
            childSession.session_id,
          );

          next.activeGatewayWorkspaceId = workspaceId;
          next.currentSessionWorkspaceId = workspaceId;
          next.workspaceRoot = workspace?.root_path ?? prev.workspaceRoot;
          next.workspaceName = workspace?.name ?? prev.workspaceName;
          next.sessions = workspaceSessions;
          next.sessionsByWorkspace.set(workspaceId, workspaceSessions);
          next.sessionGatewayWorkspaceById.set(cacheKey, workspaceId);
          next.currentSession = childSession;
          next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
          next.traceEvents = [];
          next.llmRequestLogs = [];
          next.llmRequestLogsLoadedAt = null;
          next.llmRequestLogsLoading = false;
          next.llmRequestLogsError = null;
          next.sessionResources = [];
          next.sessionResourcesLoadedAt = null;
          next.sessionResourcesLoading = false;
          next.sessionResourcesError = null;
          next.pendingConversations.delete(cacheKey);
          next.contentView = "default";
          next.status = `已从上下文创建子会话: ${childSession.title}`;
          Object.assign(next, resetAgentStateFields(next));
          writeLastSessionId(childSession.session_id);
          appendFrontendEvent(
            next.eventQueuesBySession,
            childSession.session_id,
            "session_context_forked",
            "从上下文创建子会话",
            {
              session_id: childSession.session_id,
              parent_session_id: sourceSessionId,
            },
            childSession.title,
            cacheKey,
          );
          return next;
        });
      } catch (error) {
        const refreshed = await apiListSessions(apiPort, workspaceId);
        setState((prev) => {
          const next = cloneMaps(prev);
          next.sessionsByWorkspace.set(workspaceId, refreshed.items);
          if (prev.activeGatewayWorkspaceId === workspaceId) {
            next.sessions = refreshed.items;
          }
          const message = error instanceof Error ? error.message : String(error);
          next.status = `从上下文创建子会话失败: ${message}`;
          return next;
        });
        throw error;
      }
    },
    [abortCurrentStream, apiPort, invalidateAgentState, setState],
  );

  const renameSession = useCallback(
    async (sessionId: string, title: string, workspaceId?: string | null) => {
      const normalizedTitle = normalizeSessionTitle(title);
      const workspaceIdForRequest =
        workspaceId ?? currentSessionGatewayWorkspaceId;
      setState((prev) => ({ ...prev, status: "正在命名会话" }));

      try {
        const updatedSession = await apiUpdateSession(apiPort, sessionId, {
          title: normalizedTitle,
        }, workspaceIdForRequest);
        setState((prev) => {
          const next = replaceSessionMetadata(
            prev,
            updatedSession,
            workspaceIdForRequest,
          );
          next.currentSessionWorkspaceId =
            workspaceIdForRequest ?? next.currentSessionWorkspaceId;
          next.status = `已命名会话: ${updatedSession.title}`;
          const cacheKey =
            currentSessionCacheKey ??
            (workspaceIdForRequest
              ? sessionScopeKey(
                  workspaceIdForRequest,
                  updatedSession.session_id,
                )
              : updatedSession.session_id);
          appendFrontendEvent(
            next.eventQueuesBySession,
            updatedSession.session_id,
            "session_renamed",
            "命名会话",
            {
              session_id: updatedSession.session_id,
              title: updatedSession.title,
            },
            updatedSession.title,
            cacheKey,
          );
          return next;
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `会话命名失败: ${message}` }));
        throw error;
      }
    },
    [apiPort, currentSessionCacheKey, currentSessionGatewayWorkspaceId, setState],
  );

  const setSessionParent = useCallback(
    async (
      workspaceId: string,
      sessionId: string,
      parentSessionId: string | null,
    ) => {
      setState((prev) => ({
        ...prev,
        status: parentSessionId ? "正在绑定子会话" : "正在解除会话绑定",
      }));

      try {
        const updatedSession = await apiMoveSessionParent(
          apiPort,
          workspaceId,
          sessionId,
          parentSessionId,
        );
        setState((prev) => {
          const next = replaceSessionMetadata(prev, updatedSession, workspaceId);
          next.status = parentSessionId
            ? `已将「${updatedSession.title}」绑定为子会话`
            : `已解除「${updatedSession.title}」的父会话绑定`;
          return next;
        });
      } catch (error) {
        const refreshed = await apiListSessions(apiPort, workspaceId);
        setState((prev) => {
          const next = cloneMaps(prev);
          next.sessionsByWorkspace.set(workspaceId, refreshed.items);
          if (prev.activeGatewayWorkspaceId === workspaceId) {
            next.sessions = refreshed.items;
            const currentId = prev.currentSession?.session_id;
            next.currentSession = currentId
              ? refreshed.items.find((item) => item.session_id === currentId) ?? null
              : null;
          }
          const message = error instanceof Error ? error.message : String(error);
          next.status = `更新会话树失败: ${message}`;
          return next;
        });
        throw error;
      }
    },
    [apiPort, setState],
  );

  const deleteSession = useCallback(
    async (sessionId: string, workspaceId?: string | null) => {
      const deletingCurrent = currentSession?.session_id === sessionId;
      if (deletingCurrent) {
        abortCurrentStream();
        invalidateAgentState();
      }

      setState((prev) => ({ ...prev, status: "正在删除会话" }));

      try {
        const workspaceIdForRequest =
          workspaceId ?? currentSessionGatewayWorkspaceId ?? activeGatewayWorkspaceId;
        const result = await apiDeleteSession(
          apiPort,
          sessionId,
          workspaceIdForRequest,
          true,
        );
        const refreshed = await apiListSessions(apiPort, workspaceIdForRequest);
        setState((prev) => {
          const next = cloneMaps(prev);
          const workspaceId =
            workspaceIdForRequest ??
            prev.activeGatewayWorkspaceId ??
            "workspace";
          const remainingSessions = refreshed.items;
          next.sessions = remainingSessions;
          next.sessionsByWorkspace.set(
            workspaceId,
            refreshed.items,
          );
          const remainingIds = new Set(refreshed.items.map((session) => session.session_id));
          const removedIds = (prev.sessionsByWorkspace.get(workspaceId) ?? prev.sessions)
            .map((session) => session.session_id)
            .filter((candidateId) => !remainingIds.has(candidateId));
          for (const removedId of removedIds) {
            const cacheKey = sessionScopeKey(workspaceId, removedId);
            next.sessionAttachmentSummaries.delete(removedId);
            next.eventQueuesBySession.delete(cacheKey);
            next.pendingConversations.delete(cacheKey);
            next.activeJobIdsBySession.delete(cacheKey);
            next.unreadSessionKeys.delete(cacheKey);
            next.sessionGatewayWorkspaceById.delete(cacheKey);
          }

          if (
            prev.currentSession
            && !remainingIds.has(prev.currentSession.session_id)
          ) {
            const nextSession = remainingSessions[0] ?? null;
            next.currentSession = nextSession;
            next.currentSessionWorkspaceId = nextSession ? workspaceId : null;
            next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
            next.traceEvents = [];
            next.llmRequestLogs = [];
            next.llmRequestLogsLoadedAt = null;
            next.llmRequestLogsLoading = false;
            next.llmRequestLogsError = null;
            next.sessionResources = [];
            next.sessionResourcesLoadedAt = null;
            next.sessionResourcesLoading = false;
            next.sessionResourcesError = null;
            next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
            Object.assign(next, resetAgentStateFields(next));
            if (nextSession) {
              writeLastSessionId(nextSession.session_id);
            } else {
              clearLastSessionId();
            }
          }

          next.status = `已删除会话: ${result.session_id}`;
          return next;
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `删除会话失败: ${message}` }));
        throw error;
      }
    },
    [
      abortCurrentStream,
      activeGatewayWorkspaceId,
      apiPort,
      currentSession?.session_id,
      currentSessionGatewayWorkspaceId,
      invalidateAgentState,
      setState,
    ],
  );

  const switchAgent = useCallback(
    async (agentId: string) => {
      const session = currentSession;
      if (!session) {
        throw new Error("当前没有可切换 Agent 的会话");
      }

      if (agentId === session.current_agent_id) {
        setState((prev) => ({ ...prev, status: `当前已是 Agent: ${agentId}` }));
        return;
      }

      setState((prev) => ({ ...prev, status: `正在切换 Agent: ${agentId}` }));

      try {
        const updatedSession = await apiUpdateSessionAgent(
          apiPort,
          session.session_id,
          agentId,
          currentSessionGatewayWorkspaceId,
        );
        setState((prev) => {
          const next = cloneMaps(prev);
          next.currentSession = updatedSession;
          next.currentSessionWorkspaceId =
            currentSessionGatewayWorkspaceId ??
            prev.currentSessionWorkspaceId ??
            null;
          next.sessions = prev.sessions.map((item) =>
            item.session_id === updatedSession.session_id
              ? updatedSession
              : item,
          );
          const workspaceId =
            currentSessionGatewayWorkspaceId ??
            prev.activeGatewayWorkspaceId ??
            updatedSession.workspace_id;
          const cacheKey = sessionScopeKey(workspaceId, updatedSession.session_id);
          next.sessionGatewayWorkspaceById.set(cacheKey, workspaceId);
          next.sessionsByWorkspace.set(
            workspaceId,
            (prev.sessionsByWorkspace.get(workspaceId) ?? []).map((item) =>
              item.session_id === updatedSession.session_id
                ? updatedSession
                : item,
            ),
          );
          if (
            !next.sessions.some(
              (item) => item.session_id === updatedSession.session_id,
            )
          ) {
            next.sessions = [updatedSession, ...next.sessions];
          }
          next.status = `已切换 Agent: ${updatedSession.current_agent_id}`;
          appendFrontendEvent(
            next.eventQueuesBySession,
            updatedSession.session_id,
            "agent_switched",
            "切换 Agent",
            {
              session_id: updatedSession.session_id,
              agent_id: updatedSession.current_agent_id,
            },
            updatedSession.current_agent_id,
            cacheKey,
          );
          return next;
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `Agent 切换失败: ${message}` }));
        throw error;
      }
    },
    [apiPort, currentSession, currentSessionGatewayWorkspaceId, setState],
  );

  const switchModel = useCallback(
    async (providerId: string) => {
      const session = currentSession ?? await createSession(DEFAULT_SESSION_TITLE);
      if (providerId === session.current_provider_id) {
        setState((prev) => ({
          ...prev,
          status: `当前已使用模型 provider: ${providerId}`,
        }));
        return;
      }

      setState((prev) => ({
        ...prev,
        status: `正在切换模型 provider: ${providerId}`,
      }));
      try {
        const updatedSession = await apiUpdateSessionProvider(
          apiPort,
          session.session_id,
          providerId,
          currentSessionGatewayWorkspaceId
            ?? activeGatewayWorkspaceId
            ?? defaultGatewayWorkspaceId,
        );
        setState((prev) => {
          const next = replaceSessionMetadata(
            prev,
            updatedSession,
            currentSessionGatewayWorkspaceId
              ?? activeGatewayWorkspaceId
              ?? defaultGatewayWorkspaceId,
          );
          next.status = `已切换模型 provider: ${updatedSession.current_provider_id}`;
          const workspaceId =
            currentSessionGatewayWorkspaceId
            ?? activeGatewayWorkspaceId
            ?? defaultGatewayWorkspaceId
            ?? prev.activeGatewayWorkspaceId
            ?? updatedSession.workspace_id;
          const cacheKey = sessionScopeKey(workspaceId, updatedSession.session_id);
          appendFrontendEvent(
            next.eventQueuesBySession,
            updatedSession.session_id,
            "model_switched",
            "切换模型",
            {
              session_id: updatedSession.session_id,
              provider_id: updatedSession.current_provider_id,
            },
            updatedSession.current_provider_id ?? "",
            cacheKey,
          );
          return next;
        });
      } catch (error) {
        let message = error instanceof Error ? error.message : String(error);
        try {
          const refreshed = await apiGetSession(
            apiPort,
            session.session_id,
            currentSessionGatewayWorkspaceId
              ?? activeGatewayWorkspaceId
              ?? defaultGatewayWorkspaceId,
          );
          setState((prev) => {
            const next = replaceSessionMetadata(
              prev,
              refreshed,
              currentSessionGatewayWorkspaceId
                ?? activeGatewayWorkspaceId
                ?? defaultGatewayWorkspaceId,
            );
            next.status = `模型切换失败: ${message}`;
            return next;
          });
        } catch (reconciliationError) {
          const reconciliationMessage = reconciliationError instanceof Error
            ? reconciliationError.message
            : String(reconciliationError);
          message = `${message}；重新读取会话也失败: ${reconciliationMessage}`;
          setState((prev) => ({ ...prev, status: `模型切换失败: ${message}` }));
        }
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      createSession,
      currentSession,
      currentSessionGatewayWorkspaceId,
      defaultGatewayWorkspaceId,
      setState,
    ],
  );

  const setWorkspaceDefaultAgent = useCallback(
    async (agentId: string) => {
      const workspaceId =
        currentSessionGatewayWorkspaceId
        ?? activeGatewayWorkspaceId
        ?? defaultGatewayWorkspaceId;
      if (!workspaceId) {
        throw new Error("当前没有可保存默认 Agent 的工作区");
      }
      setState((prev) => ({
        ...prev,
        status: `正在设置工作区默认 Agent: ${agentId}`,
      }));
      try {
        const agents = await apiSetWorkspaceDefaultAgent(
          apiPort,
          agentId,
          workspaceId,
        );
        setState((prev) => ({
          ...prev,
          agents,
          status: `已将 ${agentId} 设为工作区默认 Agent，仅影响新会话`,
        }));
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        try {
          const agents = await apiListAgents(apiPort, workspaceId);
          setState((prev) => ({
            ...prev,
            agents,
            status: `设置工作区默认 Agent 失败: ${message}`,
          }));
        } catch (reconciliationError) {
          const reconciliationMessage = reconciliationError instanceof Error
            ? reconciliationError.message
            : String(reconciliationError);
          setState((prev) => ({
            ...prev,
            status: `设置工作区默认 Agent 失败: ${message}；重新读取 Agent 也失败: ${reconciliationMessage}`,
          }));
        }
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      currentSessionGatewayWorkspaceId,
      defaultGatewayWorkspaceId,
      setState,
    ],
  );

  const setWorkspaceDefaultProvider = useCallback(
    async (agentId: string, providerId: string) => {
      const workspaceId =
        currentSessionGatewayWorkspaceId
        ?? activeGatewayWorkspaceId
        ?? defaultGatewayWorkspaceId;
      if (!workspaceId) {
        throw new Error("当前没有可保存默认模型的工作区");
      }
      setState((prev) => ({
        ...prev,
        status: `正在设置工作区默认模型: ${providerId}`,
      }));
      try {
        const agents = await apiSetWorkspaceDefaultProvider(
          apiPort,
          agentId,
          providerId,
          workspaceId,
        );
        setState((prev) => ({
          ...prev,
          agents,
          status: `已将 ${providerId} 设为 ${agentId} 的工作区默认模型，仅影响新会话`,
        }));
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        try {
          const agents = await apiListAgents(apiPort, workspaceId);
          setState((prev) => ({
            ...prev,
            agents,
            status: `设置工作区默认模型失败: ${message}`,
          }));
        } catch (reconciliationError) {
          const reconciliationMessage = reconciliationError instanceof Error
            ? reconciliationError.message
            : String(reconciliationError);
          setState((prev) => ({
            ...prev,
            status: `设置工作区默认模型失败: ${message}；重新读取 Agent 也失败: ${reconciliationMessage}`,
          }));
        }
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      currentSessionGatewayWorkspaceId,
      defaultGatewayWorkspaceId,
      setState,
    ],
  );

  return {
    createSession,
    deleteSession,
    forkSessionContext,
    startNewSessionDraft,
    renameSession,
    setSessionParent,
    selectSession,
    selectWorkspaceSession,
    switchAgent,
    switchModel,
    setWorkspaceDefaultAgent,
    setWorkspaceDefaultProvider,
  };
}
