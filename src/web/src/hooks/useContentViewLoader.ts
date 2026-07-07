import { useCallback } from "react";
import type { Session } from "../types/backend";
import type { ConversationContentView } from "../types/frontend";
import { resetAgentStateFields, useAgentStateSnapshotLoader } from "./useAgentStateSnapshot";
import { useRequestLogLoader } from "./useRequestLogLoader";
import { useSessionResourceLoader } from "./useSessionResourceLoader";
import type { SetAppState } from "./contentViewLoaderTypes";

export function useContentViewLoader({
  apiPort,
  currentSession,
  setState,
}: {
  apiPort: number;
  currentSession: Session | null;
  setState: SetAppState;
}) {
  const {
    invalidateAgentState,
    refreshAgentStateSnapshot,
  } = useAgentStateSnapshotLoader({ apiPort, setState });
  const {
    invalidateLLMRequestLogs,
    refreshLLMRequestLogs,
  } = useRequestLogLoader({ apiPort, setState });
  const {
    controlSessionResource,
    invalidateSessionResources,
    refreshSessionResources,
  } = useSessionResourceLoader({ apiPort, currentSession, setState });

  const switchContentView = useCallback(
    async (view: ConversationContentView) => {
      if (view === "default") {
        invalidateAgentState();
        invalidateLLMRequestLogs();
        invalidateSessionResources();
        setState((prev) => ({
          ...prev,
          contentView: "default",
          agentStateLoading: false,
          agentStateError: null,
          llmRequestLogsLoading: false,
          llmRequestLogsError: null,
          sessionResourcesLoading: false,
          sessionResourcesError: null,
          status: "默认视图",
        }));
        return;
      }

      if (view === "events") {
        invalidateAgentState();
        invalidateLLMRequestLogs();
        invalidateSessionResources();
        setState((prev) => ({
          ...prev,
          contentView: "events",
          agentStateLoading: false,
          agentStateError: null,
          llmRequestLogsLoading: false,
          llmRequestLogsError: null,
          sessionResourcesLoading: false,
          sessionResourcesError: null,
          status: "事件视图",
        }));
        return;
      }

      if (view === "requests") {
        invalidateAgentState();
        invalidateSessionResources();
        if (!currentSession) {
          setState((prev) => ({
            ...prev,
            contentView: "requests",
            llmRequestLogs: [],
            llmRequestLogsLoadedAt: new Date().toISOString(),
            llmRequestLogsLoading: false,
            llmRequestLogsError: "当前没有会话可读取 LLM 请求响应日志",
            status: "没有会话可读取 LLM 请求响应日志",
          }));
          return;
        }
        await refreshLLMRequestLogs(currentSession.session_id);
        return;
      }

      if (view === "resources") {
        invalidateAgentState();
        invalidateLLMRequestLogs();
        if (!currentSession) {
          setState((prev) => ({
            ...prev,
            contentView: "resources",
            sessionResources: [],
            sessionResourcesLoadedAt: new Date().toISOString(),
            sessionResourcesLoading: false,
            sessionResourcesError: "当前没有会话可读取资源",
            status: "没有会话可读取资源",
          }));
          return;
        }
        await refreshSessionResources(currentSession.session_id);
        return;
      }

      if (!currentSession) {
        invalidateAgentState();
        setState((prev) => ({
          ...resetAgentStateFields(prev, {
            loadedAt: new Date().toISOString(),
            error: "当前没有会话可读取 Agent State",
          }),
          contentView: "agent",
          status: "没有会话可读取 Agent State",
        }));
        return;
      }

      await refreshAgentStateSnapshot(currentSession.session_id);
    },
    [
      currentSession,
      invalidateAgentState,
      invalidateLLMRequestLogs,
      invalidateSessionResources,
      refreshAgentStateSnapshot,
      refreshLLMRequestLogs,
      refreshSessionResources,
      setState,
    ],
  );

  return {
    controlSessionResource,
    invalidateAgentState,
    refreshAgentStateSnapshot,
    refreshLLMRequestLogs,
    refreshSessionResources,
    switchContentView,
  };
}
