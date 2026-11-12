import { t } from "../../../../shared/core/i18n/index.js";
import { useCallback } from "react";
import type { Session } from "../../types/backend";
import type { ConversationContentView } from "../../types/frontend";
import { resetAgentStateFields, useAgentStateSnapshotLoader } from "../runtime/useAgentStateSnapshot";
import { useRequestLogLoader } from "../runtime/useRequestLogLoader";
import { useSessionChangesLoader } from "../session/useSessionChangesLoader";
import { useSessionResourceLoader } from "../session/useSessionResourceLoader";
import type { SetAppState } from "../contentViewLoaderTypes";

export function useContentViewLoader({
  apiPort,
  currentSession,
  currentSessionGatewayWorkspaceId,
  setState,
  displayChangesInChat = true,
}: {
  apiPort: number;
  currentSession: Session | null;
  currentSessionGatewayWorkspaceId: string | null;
  setState: SetAppState;
  displayChangesInChat?: boolean;
}) {
  const {
    invalidateAgentState,
    loadAgentStateMessageRawContent,
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
    loadSessionChangesets,
    refreshSessionChanges,
    reviewSessionChangeFile,
  } = useSessionChangesLoader({
    apiPort,
    currentSession,
    workspaceId: currentSessionGatewayWorkspaceId,
    setState,
    displayInChat: displayChangesInChat,
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
      if (view !== "requests") invalidateLLMRequestLogs();
      if (view === "default") {
        invalidateAgentState();
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
          status: t("content.default"),
        }));
        return;
      }

      if (view === "events") {
        invalidateAgentState();
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
          status: t("content.events"),
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
            llmRequestLogsError: t("content.noRequests"),
            status: t("content.noRequestsStatus"),
          }));
          return;
        }
        setState((prev) => ({
          ...prev,
          contentView: "requests",
          llmRequestLogsLoading: true,
          llmRequestLogsError: null,
          status: t("content.loadingRequests"),
        }));
        return;
      }

      if (view === "changes") {
        invalidateAgentState();
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
            sessionChangesError: t("content.noChanges"),
            status: t("content.noChangesStatus"),
          }));
          return;
        }
        setState((prev) => ({
          ...prev,
          contentView: "changes",
          sessionChangesLoading: true,
          sessionChangesError: null,
          status: t("content.loadingChanges"),
        }));
        return;
      }

      if (view === "resources") {
        invalidateAgentState();
        invalidateSessionChanges();
        if (!currentSession) {
          setState((prev) => ({
            ...prev,
            contentView: "resources",
            sessionResources: [],
            sessionResourcesLoadedAt: new Date().toISOString(),
            sessionResourcesLoading: false,
            sessionResourcesError: null,
            status: t("content.noResourcesStatus"),
          }));
          return;
        }
        setState((prev) => ({
          ...prev,
          contentView: "resources",
          sessionResourcesLoading: true,
          sessionResourcesError: null,
          status: t("content.loadingResources"),
        }));
        return;
      }

      if (!currentSession) {
        invalidateAgentState();
        setState((prev) => ({
          ...resetAgentStateFields(prev, {
            loadedAt: new Date().toISOString(),
            error: t("content.noContext"),
          }),
          contentView: "agent",
          status: t("content.noContextStatus"),
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
    loadAgentStateMessageRawContent,
    loadSessionChangesets,
    refreshSessionChanges,
    refreshAgentStateSnapshot,
    refreshLLMRequestLogs,
    refreshSessionResources,
    reviewSessionChangeFile,
    switchContentView,
  };
}
