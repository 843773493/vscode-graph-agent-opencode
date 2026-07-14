import { useCallback } from "react";
import {
  createSession as apiCreateSession,
  DEFAULT_SESSION_TITLE,
  deleteSession as apiDeleteSession,
  forkSessionContext as apiForkSessionContext,
  listSessions as apiListSessions,
  updateSession as apiUpdateSession,
  updateSessionAgent as apiUpdateSessionAgent,
} from "../api";
import type { Session } from "../types/backend";
import { cloneMaps } from "../state/appStateMaps";
import {
  clearLastSessionId,
  writeLastSessionId,
} from "../state/storage";
import { replaceSessionMetadata } from "../state/sessions";
import { appendFrontendEvent } from "../state/traceEvents";
import { resetAgentStateFields } from "./useAgentStateSnapshot";
import type { SetAppState } from "./contentViewLoaderTypes";
import { sessionScopeKey } from "../state/sessionScope";

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
        next.messages = [];
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
        next.messages = [];
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
    async (title: string = DEFAULT_SESSION_TITLE) => {
      invalidateAgentState();
      const normalizedTitle = normalizeSessionTitle(title);
      try {
        const session = await apiCreateSession(
          apiPort,
          normalizedTitle,
          defaultGatewayWorkspaceId,
        );
        setState((prev) => {
          const next = cloneMaps(prev);
          const resolvedWorkspaceId =
            defaultGatewayWorkspaceId ??
            prev.activeGatewayWorkspaceId;
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
          next.messages = [];
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
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `创建会话失败: ${message}` }));
        throw error;
      }
    },
    [apiPort, defaultGatewayWorkspaceId, invalidateAgentState, setState],
  );

  const startNewSessionDraft = useCallback((workspaceId?: string | null) => {
    abortCurrentStream();
    invalidateAgentState();
    clearLastSessionId();
    setState((prev) => {
      const next = cloneMaps(prev);
      const targetWorkspaceId = workspaceId ?? defaultGatewayWorkspaceId;
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
      next.messages = [];
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
          next.messages = [];
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
    async (sessionId: string, title: string) => {
      const normalizedTitle = normalizeSessionTitle(title);
      setState((prev) => ({ ...prev, status: "正在命名会话" }));

      try {
        const updatedSession = await apiUpdateSession(apiPort, sessionId, {
          title: normalizedTitle,
        }, currentSessionGatewayWorkspaceId);
        setState((prev) => {
          const next = replaceSessionMetadata(
            prev,
            updatedSession,
            currentSessionGatewayWorkspaceId,
          );
          next.currentSessionWorkspaceId =
            currentSessionGatewayWorkspaceId ?? next.currentSessionWorkspaceId;
          next.status = `已命名会话: ${updatedSession.title}`;
          const cacheKey =
            currentSessionCacheKey ??
            (currentSessionGatewayWorkspaceId
              ? sessionScopeKey(
                  currentSessionGatewayWorkspaceId,
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
        const updatedSession = await apiUpdateSession(
          apiPort,
          sessionId,
          { parent_session_id: parentSessionId },
          workspaceId,
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
    async (sessionId: string) => {
      const deletingCurrent = currentSession?.session_id === sessionId;
      if (deletingCurrent) {
        abortCurrentStream();
        invalidateAgentState();
      }

      setState((prev) => ({ ...prev, status: "正在删除会话" }));

      try {
        const workspaceIdForRequest =
          currentSessionGatewayWorkspaceId ?? activeGatewayWorkspaceId;
        const result = await apiDeleteSession(apiPort, sessionId, workspaceIdForRequest);
        setState((prev) => {
          const next = cloneMaps(prev);
          const workspaceId =
            workspaceIdForRequest ??
            prev.activeGatewayWorkspaceId ??
            "workspace";
          const remainingSessions = prev.sessions.filter(
            (session) => session.session_id !== sessionId,
          );
          next.sessions = remainingSessions;
          next.sessionsByWorkspace.set(
            workspaceId,
            (prev.sessionsByWorkspace.get(workspaceId) ?? []).filter(
              (session) => session.session_id !== sessionId,
            ),
          );
          const cacheKey = sessionScopeKey(workspaceId, sessionId);
          next.sessionAttachmentSummaries.delete(sessionId);
          next.eventQueuesBySession.delete(cacheKey);
          next.pendingConversations.delete(cacheKey);
          next.sessionGatewayWorkspaceById.delete(cacheKey);

          if (prev.currentSession?.session_id === sessionId) {
            const nextSession = remainingSessions[0] ?? null;
            next.currentSession = nextSession;
            next.currentSessionWorkspaceId = nextSession ? workspaceId : null;
            next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
            next.messages = [];
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
  };
}
