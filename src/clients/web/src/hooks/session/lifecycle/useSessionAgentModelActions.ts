import { useCallback } from "react";
import {
  DEFAULT_SESSION_TITLE,
  getSession as apiGetSession,
  updateSessionAgent as apiUpdateSessionAgent,
  updateSessionProvider as apiUpdateSessionProvider,
} from "../../../api";
import { replaceSessionMetadata } from "../../../state/session/sessions";
import { appendFrontendEvent } from "../../../state/traceEvents";
import { sessionScopeKey } from "../../../state/session/sessionScope";
import { errorMessage } from "../../../utils/errorMessage";
import type { SessionLifecycleContext } from "./sessionLifecycleContext";
import type { useSessionCreationActions } from "./useSessionCreationActions";
import { currentSessionMatchesOwner } from "./sessionLifecycleTransitions";

export function useSessionAgentModelActions(
  context: SessionLifecycleContext,
  createSession: ReturnType<typeof useSessionCreationActions>["createSession"],
) {
  const {
    apiPort, currentSession, currentSessionGatewayWorkspaceId,
    activeGatewayWorkspaceId, defaultGatewayWorkspaceId, setState,
  } = context;

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

      const statusOwner = {
        sessionId: session.session_id,
        workspaceId: currentSessionGatewayWorkspaceId ?? activeGatewayWorkspaceId,
      };

      setState((prev) => ({ ...prev, status: `正在切换 Agent: ${agentId}` }));

      try {
        const updatedSession = await apiUpdateSessionAgent(
          apiPort,
          session.session_id,
          agentId,
          currentSessionGatewayWorkspaceId,
        );
        setState((prev) => {
          // 只改会话元数据，且仅当用户仍停在被切换的会话上时才写
          // currentSession（replaceSessionMetadata 的内部守卫）。直接赋值会把
          // 请求在途期间用户已经切走的会话拉回来。
          const workspaceId =
            currentSessionGatewayWorkspaceId ??
            prev.activeGatewayWorkspaceId ??
            updatedSession.workspace_id;
          const next = replaceSessionMetadata(prev, updatedSession, workspaceId);
          if (currentSessionMatchesOwner(prev, statusOwner)) {
            next.status = `已切换 Agent: ${updatedSession.current_agent_id}`;
          }
          const cacheKey = sessionScopeKey(workspaceId, updatedSession.session_id);
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
        let message = errorMessage(error);
        // 失败后重取会话校准：切换可能已生效（如响应超时），也可能是别的
        // 来源改了 current_agent_id，本地镜像必须以后端真值为准。
        try {
          const refreshed = await apiGetSession(
            apiPort,
            session.session_id,
            currentSessionGatewayWorkspaceId,
          );
          setState((prev) => {
            const next = replaceSessionMetadata(
              prev,
              refreshed,
              currentSessionGatewayWorkspaceId,
            );
            if (currentSessionMatchesOwner(prev, statusOwner)) {
              next.status = `Agent 切换失败: ${message}`;
            }
            return next;
          });
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          message = `${message}；重新读取会话也失败: ${reconciliationMessage}`;
          setState((prev) => currentSessionMatchesOwner(prev, statusOwner)
            ? { ...prev, status: `Agent 切换失败: ${message}` }
            : prev);
        }
        throw error;
      }
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      currentSession,
      currentSessionGatewayWorkspaceId,
      setState,
    ],
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

      const statusOwner = {
        sessionId: session.session_id,
        workspaceId:
          currentSessionGatewayWorkspaceId
          ?? activeGatewayWorkspaceId
          ?? defaultGatewayWorkspaceId,
      };

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
          if (currentSessionMatchesOwner(prev, statusOwner)) {
            next.status = `已切换模型 provider: ${updatedSession.current_provider_id}`;
          }
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
        let message = errorMessage(error);
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
            if (currentSessionMatchesOwner(prev, statusOwner)) {
              next.status = `模型切换失败: ${message}`;
            }
            return next;
          });
        } catch (reconciliationError) {
          const reconciliationMessage = errorMessage(reconciliationError);
          message = `${message}；重新读取会话也失败: ${reconciliationMessage}`;
          setState((prev) => currentSessionMatchesOwner(prev, statusOwner)
            ? { ...prev, status: `模型切换失败: ${message}` }
            : prev);
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

  return { switchAgent, switchModel };
}
