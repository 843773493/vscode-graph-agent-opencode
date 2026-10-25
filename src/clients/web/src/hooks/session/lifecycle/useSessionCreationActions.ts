import { useCallback } from "react";
import {
  createSession as apiCreateSession,
  DEFAULT_SESSION_TITLE,
  forkSessionContext as apiForkSessionContext,
  listSessions as apiListSessions,
} from "../../../api";
import { getGatewayHealth } from "../../../gatewayApi";
import type { SessionForkRequest } from "../../../types/backend";
import { cloneMaps } from "../../../state/appStateMaps";
import { writeLastSessionId } from "../../../state/storage";
import { sessionScopeKey } from "../../../state/session/sessionScope";
import { appendFrontendEvent } from "../../../state/traceEvents";
import { resetAgentStateFields } from "../../runtime/useAgentStateSnapshot";
import { resetSessionScopedFields } from "../sessionScopedState";
import { errorMessage } from "../../../utils/errorMessage";
import { requireForkTargetWorkspace } from "../../../api/workspaceIdentity";
import type { SessionLifecycleContext } from "./sessionLifecycleContext";
import {
  currentSessionMatchesOwner,
  normalizeSessionTitle,
} from "./sessionLifecycleTransitions";

export function useSessionCreationActions(context: SessionLifecycleContext) {
  const {
    apiPort, activeGatewayWorkspaceId, defaultGatewayWorkspaceId,
    invalidateAgentState, setState, selectionIntentRef, workspaceRequestTargets,
  } = context;

  const createSession = useCallback(
    async (
      title: string = DEFAULT_SESSION_TITLE,
      workspaceId?: string | null,
      folderId?: string | null,
    ) => {
      const selectionIntent = ++selectionIntentRef.current;
      invalidateAgentState();
      const targetWorkspaceId =
        workspaceId ?? activeGatewayWorkspaceId ?? defaultGatewayWorkspaceId;
      try {
        // 校验必须在 try 内：否则空标题同步抛出，绕过下面统一失败上报，
        // 调用方拿到一个没有对应 status 的错误。
        const normalizedTitle = normalizeSessionTitle(title);
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
          const previousWorkspaceSessions = resolvedWorkspaceId
            ? prev.sessionsByWorkspace.get(resolvedWorkspaceId) ?? []
            : prev.sessions;
          const workspaceSessions = [
            session,
            ...previousWorkspaceSessions.filter(
              (item) => item.session_id !== session.session_id,
            ),
          ];
          if (resolvedWorkspaceId) {
            next.sessionsByWorkspace.set(resolvedWorkspaceId, workspaceSessions);
            next.sessionGatewayWorkspaceById.set(
              sessionScopeKey(resolvedWorkspaceId, session.session_id),
              resolvedWorkspaceId,
            );
          }
          if (prev.activeGatewayWorkspaceId === resolvedWorkspaceId) next.sessions = workspaceSessions;
          if (selectionIntent === selectionIntentRef.current) {
            next.activeGatewayWorkspaceId = resolvedWorkspaceId;
            next.currentSessionWorkspaceId = resolvedWorkspaceId ?? null;
            next.workspaceName = workspace?.name ?? prev.workspaceName;
            next.sessions = workspaceSessions;
            next.sessionHistoryReloadNonce = prev.sessionHistoryReloadNonce + 1;
            next.currentSession = session;
            writeLastSessionId(session.session_id);
            Object.assign(next, resetSessionScopedFields(next));
            next.status = "已创建会话";
            next.contentView = "default";
            Object.assign(next, resetAgentStateFields(next));
          }
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
        const message = errorMessage(error);
        setState((prev) => selectionIntent === selectionIntentRef.current
          ? { ...prev, status: `创建会话失败: ${message}` } : prev);
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

  const forkSessionContext = useCallback(
    async (workspaceId: string, sourceSessionId: string, request: SessionForkRequest) => {
      const selectionIntent = ++selectionIntentRef.current;
      setState((prev) => ({
        ...prev,
        status: "正在创建上下文分支",
      }));

      try {
        const health = await getGatewayHealth(apiPort);
        const result = await apiForkSessionContext(
          apiPort,
          sourceSessionId,
          request,
          workspaceId,
        );
        const targetWorkspaceId = requireForkTargetWorkspace(
          workspaceRequestTargets, health.gateway_id, result,
        ).workspace_id;
        setState((prev) => {
          const next = cloneMaps(prev);
          const sessions = prev.sessionsByWorkspace.get(targetWorkspaceId) ?? [];
          const targetSessions = [result.session, ...sessions.filter((session) =>
            session.session_id !== result.session.session_id,
          )];
          next.sessionsByWorkspace.set(targetWorkspaceId, targetSessions);
          if (prev.activeGatewayWorkspaceId === targetWorkspaceId) next.sessions = targetSessions;
          if (currentSessionMatchesOwner(prev, { sessionId: result.session.session_id, workspaceId: targetWorkspaceId })) {
            next.currentSession = result.session;
          }
          if (selectionIntent === selectionIntentRef.current) next.status = `已创建上下文分支: ${result.thread_id}`;
          return next;
        });
        return result;
      } catch (error) {
        const message = errorMessage(error);
        // 补偿重取失败不得覆盖原始错误：列表服务不可用时抛出的是 503，
        // 而用户真正需要看到的是 fork 失败本身（如 422 上下文快照损坏）。
        try {
          const refreshed = await apiListSessions(apiPort, workspaceId);
          setState((prev) => {
            const next = cloneMaps(prev);
            next.sessionsByWorkspace.set(workspaceId, refreshed.items);
            if (prev.activeGatewayWorkspaceId === workspaceId) {
              next.sessions = refreshed.items;
            }
            if (selectionIntent === selectionIntentRef.current) next.status = `创建上下文分支失败: ${message}`;
            return next;
          });
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          setState((prev) => selectionIntent === selectionIntentRef.current ? ({
            ...prev,
            status: `创建上下文分支失败: ${message}；重新读取会话列表也失败: ${reconciliationMessage}`,
          }) : prev);
        }
        throw error;
      }
    },
    [apiPort, setState, selectionIntentRef, workspaceRequestTargets],
  );

  return { createSession, forkSessionContext };
}
