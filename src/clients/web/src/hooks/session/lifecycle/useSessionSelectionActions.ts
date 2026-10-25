import { useCallback } from "react";
import type { Session } from "../../../types/backend";
import { cloneMaps } from "../../../state/appStateMaps";
import { sessionScopeKey } from "../../../state/session/sessionScope";
import type { SessionLifecycleContext } from "./sessionLifecycleContext";
import {
  applySessionSelectionTransition,
  clearUnreadSessionKey,
  currentSessionMatchesOwner,
} from "./sessionLifecycleTransitions";

export function useSessionSelectionActions(context: SessionLifecycleContext) {
  const { readState, setState, abortCurrentStream, invalidateAgentState, selectionIntentRef } = context;

  const selectSession = useCallback(
    (sessionId: string) => {
      selectionIntentRef.current += 1;
      const snapshot = readState();
      // 合法选择由订阅的 owner effect 清理旧连接；未知选择保留原中断合同。
      if (snapshot.currentSession?.session_id !== sessionId
        && !snapshot.sessions.some((session) => session.session_id === sessionId)) {
        abortCurrentStream();
        invalidateAgentState();
      }
      setState((prev) => {
        const workspaceId = prev.activeGatewayWorkspaceId;
        const cacheKey = sessionScopeKey(workspaceId, sessionId);
        if (prev.currentSession?.session_id === sessionId
          && (prev.currentSessionWorkspaceId ?? workspaceId) === workspaceId) {
          return clearUnreadSessionKey(prev, cacheKey);
        }
        const selected =
          prev.sessions.find((session) => session.session_id === sessionId);
        if (!selected) {
          // 未命中会话必须显式失败：旧实现用 `?? prev.currentSession` 静默回退，
          // 会带着「切换成功」的全套副作用把用户留在原会话上。
          return {
            ...prev,
            status: `切换会话失败: 不存在会话 ${sessionId}`,
          };
        }
        const next = cloneMaps(prev);
        next.currentSession = selected;
        next.currentSessionWorkspaceId = workspaceId;
        if (workspaceId) {
          next.sessionGatewayWorkspaceById.set(
            cacheKey,
            workspaceId,
          );
        }
        applySessionSelectionTransition(next, prev, selected, cacheKey, {
          session_id: selected.session_id,
          title: selected.title,
        });
        return next;
      });
    },
    [
      abortCurrentStream,
      invalidateAgentState,
      readState,
      setState,
      selectionIntentRef,
    ],
  );

  const selectWorkspaceSession = useCallback(
    (
      workspaceId: string,
      sessionId: string,
      sessionOverride?: Session,
    ) => {
      selectionIntentRef.current += 1;
      const snapshot = readState();
      const alreadyCurrent = currentSessionMatchesOwner(snapshot, { sessionId, workspaceId })
        && snapshot.activeGatewayWorkspaceId === workspaceId;
      const selected = sessionOverride ?? snapshot.sessionsByWorkspace.get(workspaceId)?.find(
        (session) => session.session_id === sessionId,
      );
      const workspaceKnown = workspaceId === snapshot.activeGatewayWorkspaceId
        || snapshot.gatewayWorkspaces.some((workspace) => workspace.workspace_id === workspaceId);
      if (!alreadyCurrent && (!selected || !workspaceKnown)) {
        abortCurrentStream();
        invalidateAgentState();
      }
      setState((prev) => {
        if (prev.currentSession?.session_id === sessionId
          && (prev.currentSessionWorkspaceId ?? prev.activeGatewayWorkspaceId) === workspaceId
          && prev.activeGatewayWorkspaceId === workspaceId) {
          return clearUnreadSessionKey(prev, sessionScopeKey(workspaceId, sessionId));
        }
        const workspaceSessions = prev.sessionsByWorkspace.get(workspaceId) ?? [];
        const selected = sessionOverride ?? workspaceSessions.find(
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
        if (!workspace && workspaceId !== prev.activeGatewayWorkspaceId) {
          // 未知身份不能沿用上一个工作区的展示信息。
          return {
            ...prev,
            status: `切换会话失败: 未知工作区 ${workspaceId}`,
          };
        }
        const next = cloneMaps(prev);
        const nextWorkspaceSessions = [
          selected,
          ...workspaceSessions.filter(
            (session) => session.session_id !== selected.session_id,
          ),
        ];
        next.activeGatewayWorkspaceId = workspaceId;
        if (workspace) {
          next.workspaceName = workspace.name;
        }
        next.sessions = nextWorkspaceSessions;
        next.sessionsByWorkspace.set(workspaceId, nextWorkspaceSessions);
        next.currentSession = selected;
        next.currentSessionWorkspaceId = workspaceId;
        const cacheKey = sessionScopeKey(workspaceId, selected.session_id);
        next.sessionGatewayWorkspaceById.set(cacheKey, workspaceId);
        next.workspaceSwitching = false;
        next.error = null;
        applySessionSelectionTransition(next, prev, selected, cacheKey, {
          session_id: selected.session_id,
          title: selected.title,
          workspace_id: workspaceId,
        });
        return next;
      });
    },
    [
      abortCurrentStream,
      invalidateAgentState,
      readState,
      setState,
      selectionIntentRef,
    ],
  );

  return { selectSession, selectWorkspaceSession };
}
