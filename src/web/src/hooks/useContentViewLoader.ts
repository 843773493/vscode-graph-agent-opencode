import { useCallback } from "react";
import type { Session } from "../types/backend";
import type { ConversationContentView } from "../types/frontend";
import { resetAgentStateFields, useAgentStateSnapshotLoader } from "./useAgentStateSnapshot";
import { useRequestLogLoader } from "./useRequestLogLoader";
import { useSessionChangesLoader } from "./useSessionChangesLoader";
import { useSessionResourceLoader } from "./useSessionResourceLoader";
import type { SetAppState } from "./contentViewLoaderTypes";

export function useContentViewLoader({
  apiPort,
  currentSession,
  currentSessionGatewayWorkspaceId,
  setState,
}: {
  apiPort: number;
  currentSession: Session | null;
  currentSessionGatewayWorkspaceId: string | null;
  setState: SetAppState;
}) {
  const {
    invalidateAgentState,
    refreshAgentStateSnapshot,
  } = useAgentStateSnapshotLoader({
    apiPort,
    workspaceId: currentSessionGatewayWorkspaceId,
    setState,
  });
  const {
    invalidateLLMRequestLogs,
    refreshLLMRequestLogs,
  } = useRequestLogLoader({
    apiPort,
    workspaceId: currentSessionGatewayWorkspaceId,
    setState,
  });
  const {
    invalidateSessionChanges,
    refreshSessionChanges,
    reviewSessionChangeFile,
  } = useSessionChangesLoader({
    apiPort,
    currentSession,
    workspaceId: currentSessionGatewayWorkspaceId,
    setState,
  });
  const {
    controlSessionResource,
    invalidateSessionResources,
    refreshSessionResources,
  } = useSessionResourceLoader({
    apiPort,
    currentSession,
    workspaceId: currentSessionGatewayWorkspaceId,
    setState,
  });

  const switchContentView = useCallback(
    async (view: ConversationContentView) => {
      if (view === "default") {
        invalidateAgentState();
        invalidateLLMRequestLogs();
        invalidateSessionChanges();
        invalidateSessionResources();
        setState((prev) => ({
          ...prev,
          contentView: "default",
          agentStateLoading: false,
          agentStateError: null,
          llmRequestLogsLoading: false,
          llmRequestLogsError: null,
          sessionChangesLoading: false,
          sessionChangesError: null,
          sessionResourcesLoading: false,
          sessionResourcesError: null,
          status: "默认视图",
        }));
        return;
      }

      if (view === "events") {
        invalidateAgentState();
        invalidateLLMRequestLogs();
        invalidateSessionChanges();
        invalidateSessionResources();
        setState((prev) => ({
          ...prev,
          contentView: "events",
          agentStateLoading: false,
          agentStateError: null,
          llmRequestLogsLoading: false,
          llmRequestLogsError: null,
          sessionChangesLoading: false,
          sessionChangesError: null,
          sessionResourcesLoading: false,
          sessionResourcesError: null,
          status: "事件视图",
        }));
        return;
      }

      if (view === "requests") {
        invalidateAgentState();
        invalidateSessionChanges();
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
        setState((prev) => ({
          ...prev,
          contentView: "requests",
          llmRequestLogsLoading: true,
          llmRequestLogsError: null,
          status: "正在读取 LLM 请求响应日志...",
        }));
        return;
      }

      if (view === "changes") {
        invalidateAgentState();
        invalidateLLMRequestLogs();
        invalidateSessionResources();
        if (!currentSession) {
          setState((prev) => ({
            ...prev,
            contentView: "changes",
            sessionChangesets: [],
            selectedChangesetId: null,
            activeChangeset: null,
            sessionChangesLoadedAt: new Date().toISOString(),
            sessionChangesLoading: false,
            sessionChangesError: "当前没有会话可读取文件变更",
            status: "没有会话可读取文件变更",
          }));
          return;
        }
        setState((prev) => ({
          ...prev,
          contentView: "changes",
          sessionChangesLoading: true,
          sessionChangesError: null,
          status: "正在读取会话文件变更...",
        }));
        return;
      }

      if (view === "resources") {
        invalidateAgentState();
        invalidateLLMRequestLogs();
        invalidateSessionChanges();
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
        setState((prev) => ({
          ...prev,
          contentView: "resources",
          sessionResourcesLoading: true,
          sessionResourcesError: null,
          status: "正在读取后台连接...",
        }));
        return;
      }

      if (!currentSession) {
        invalidateAgentState();
        setState((prev) => ({
          ...resetAgentStateFields(prev, {
            loadedAt: new Date().toISOString(),
            error: "当前没有会话可读取上下文状态",
          }),
          contentView: "agent",
          status: "没有会话可读取上下文状态",
        }));
        return;
      }

      await refreshAgentStateSnapshot(currentSession.session_id);
    },
    [
      currentSession,
      invalidateAgentState,
      invalidateSessionChanges,
      invalidateLLMRequestLogs,
      invalidateSessionResources,
      refreshAgentStateSnapshot,
      setState,
    ],
  );

  return {
    controlSessionResource,
    invalidateAgentState,
    refreshSessionChanges,
    refreshAgentStateSnapshot,
    refreshLLMRequestLogs,
    refreshSessionResources,
    reviewSessionChangeFile,
    switchContentView,
  };
}
