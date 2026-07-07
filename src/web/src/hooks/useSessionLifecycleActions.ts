import { useCallback } from "react";
import {
  createSession as apiCreateSession,
  DEFAULT_SESSION_TITLE,
  deleteSession as apiDeleteSession,
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
  setState,
  abortCurrentStream,
  invalidateAgentState,
}: {
  apiPort: number;
  currentSession: Session | null;
  setState: SetAppState;
  abortCurrentStream: () => void;
  invalidateAgentState: () => void;
}) {
  const selectSession = useCallback(
    (sessionId: string) => {
      if (currentSession?.session_id === sessionId) {
        return;
      }
      abortCurrentStream();
      invalidateAgentState();
      setState((prev) => {
        const next = cloneMaps(prev);
        const selected =
          prev.sessions.find((session) => session.session_id === sessionId) ??
          prev.currentSession;
        next.currentSession = selected;
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
        next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
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
          );
        }
        return next;
      });
    },
    [abortCurrentStream, currentSession?.session_id, invalidateAgentState, setState],
  );

  const createSession = useCallback(
    async (title: string = DEFAULT_SESSION_TITLE) => {
      invalidateAgentState();
      const normalizedTitle = normalizeSessionTitle(title);
      try {
        const session = await apiCreateSession(apiPort, normalizedTitle);
        setState((prev) => {
          const next = cloneMaps(prev);
          next.sessions = [session, ...prev.sessions];
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
          );
          return next;
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `创建会话失败: ${message}` }));
        throw error;
      }
    },
    [apiPort, invalidateAgentState, setState],
  );

  const renameSession = useCallback(
    async (sessionId: string, title: string) => {
      const normalizedTitle = normalizeSessionTitle(title);
      setState((prev) => ({ ...prev, status: "正在命名会话" }));

      try {
        const updatedSession = await apiUpdateSession(apiPort, sessionId, {
          title: normalizedTitle,
        });
        setState((prev) => {
          const next = replaceSessionMetadata(prev, updatedSession);
          next.status = `已命名会话: ${updatedSession.title}`;
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
          );
          return next;
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `会话命名失败: ${message}` }));
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
        const result = await apiDeleteSession(apiPort, sessionId);
        setState((prev) => {
          const next = cloneMaps(prev);
          const remainingSessions = prev.sessions.filter(
            (session) => session.session_id !== sessionId,
          );
          next.sessions = remainingSessions;
          next.sessionAttachmentSummaries.delete(sessionId);
          next.eventQueuesBySession.delete(sessionId);
          next.pendingConversations.delete(sessionId);

          if (prev.currentSession?.session_id === sessionId) {
            const nextSession = remainingSessions[0] ?? null;
            next.currentSession = nextSession;
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
      apiPort,
      currentSession?.session_id,
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
        );
        setState((prev) => {
          const next = cloneMaps(prev);
          next.currentSession = updatedSession;
          next.sessions = prev.sessions.map((item) =>
            item.session_id === updatedSession.session_id
              ? updatedSession
              : item,
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
          );
          return next;
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `Agent 切换失败: ${message}` }));
        throw error;
      }
    },
    [apiPort, currentSession, setState],
  );

  return {
    createSession,
    deleteSession,
    renameSession,
    selectSession,
    switchAgent,
  };
}
