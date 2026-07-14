import { useCallback } from "react";
import {
  compactSessionContext as apiCompactSessionContext,
  createSession as apiCreateSession,
  DEFAULT_SESSION_TITLE,
  interruptSession as apiInterruptSession,
  sendUserMessage as apiSendMessage,
} from "../api";
import type { AttachmentRef, MessageRunAccepted, Session } from "../types/backend";
import type { ConversationContentView, ConversationView } from "../types/frontend";
import { cloneMaps } from "../state/appStateMaps";
import { updateSessionAttachmentSummary } from "../state/attachments";
import { writePendingList } from "../state/conversations";
import { appendFrontendEvent } from "../state/traceEvents";
import { writeLastSessionId } from "../state/storage";
import type { SetAppState } from "./contentViewLoaderTypes";
import { sessionScopeKey } from "../state/sessionScope";

export function useSessionRunActions({
  apiPort,
  currentSession,
  activeGatewayWorkspaceId,
  currentSessionGatewayWorkspaceId,
  currentSessionCacheKey,
  defaultGatewayWorkspaceId,
  contentView,
  setState,
  refreshAgentStateSnapshot,
}: {
  apiPort: number;
  currentSession: Session | null;
  activeGatewayWorkspaceId: string | null;
  currentSessionGatewayWorkspaceId: string | null;
  currentSessionCacheKey: string | null;
  defaultGatewayWorkspaceId: string | null;
  contentView: ConversationContentView;
  setState: SetAppState;
  refreshAgentStateSnapshot: (sessionId: string) => Promise<void>;
}) {
  const sendMessage = useCallback(
    async (content: string, attachments: AttachmentRef[] = []) => {
      let session = currentSession;
      if (!session) {
        const targetWorkspaceId = activeGatewayWorkspaceId ?? defaultGatewayWorkspaceId;
        setState((prev) => ({ ...prev, status: "正在创建会话" }));
        try {
          session = await apiCreateSession(
            apiPort,
            DEFAULT_SESSION_TITLE,
            targetWorkspaceId,
          );
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          setState((prev) => ({ ...prev, status: `创建会话失败: ${message}` }));
          throw error;
        }
        const createdSession = session;
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
          if (resolvedWorkspaceId && workspace) {
            next.activeGatewayWorkspaceId = resolvedWorkspaceId;
            next.currentSessionWorkspaceId = resolvedWorkspaceId;
            next.workspaceRoot = workspace.root_path;
            next.workspaceName = workspace.name;
          }
          next.sessions = [
            createdSession,
            ...previousWorkspaceSessions.filter(
              (item) => item.session_id !== createdSession.session_id,
            ),
          ];
          if (resolvedWorkspaceId) {
            next.sessionsByWorkspace.set(resolvedWorkspaceId, next.sessions);
            next.sessionGatewayWorkspaceById.set(
              sessionScopeKey(resolvedWorkspaceId, createdSession.session_id),
              resolvedWorkspaceId,
            );
          }
          next.currentSession = createdSession;
          next.currentSessionWorkspaceId = resolvedWorkspaceId ?? null;
          writeLastSessionId(createdSession.session_id);
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
          appendFrontendEvent(
            next.eventQueuesBySession,
            createdSession.session_id,
            "session_created",
            "创建会话",
            {
              session_id: createdSession.session_id,
              title: createdSession.title,
            },
            createdSession.title,
            resolvedWorkspaceId
              ? sessionScopeKey(resolvedWorkspaceId, createdSession.session_id)
              : createdSession.session_id,
          );
          return next;
        });
      }

      const activeSession = session;
      const activeSessionGatewayWorkspaceId =
        currentSessionGatewayWorkspaceId ??
        activeGatewayWorkspaceId ??
        defaultGatewayWorkspaceId;
      const activeSessionCacheKey =
        currentSessionCacheKey ??
        (activeSessionGatewayWorkspaceId
          ? sessionScopeKey(activeSessionGatewayWorkspaceId, activeSession.session_id)
          : activeSession.session_id);
      const pendingSubmissionId = `pending_submission_${Date.now()}`;
      const submittedAt = new Date().toISOString();
      setState((prev) => {
        const next = cloneMaps(prev);
        const conversation: ConversationView = {
          conversationId: pendingSubmissionId,
          sessionId: activeSession.session_id,
          userMessage: {
            message_id: pendingSubmissionId,
            session_id: activeSession.session_id,
            role: "user",
            content,
            metadata: {
              source: "optimistic",
              pending_submission_id: pendingSubmissionId,
            },
            attachments,
            created_at: submittedAt,
            updated_at: submittedAt,
          },
          assistantMessages: [],
          events: [],
          status: "running",
          jobId: null,
          pending: true,
          pendingSubmissionId,
          source: "pending",
        };
        updateSessionAttachmentSummary(
          next.sessionAttachmentSummaries,
          activeSession.session_id,
          attachments,
          submittedAt,
        );
        const pendingList =
          next.pendingConversations.get(activeSessionCacheKey) ?? [];
        next.pendingConversations.set(activeSessionCacheKey, [
          ...pendingList,
          conversation,
        ]);
        next.status = "正在发送消息";
        next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
        return next;
      });

      let accepted: MessageRunAccepted;
      try {
        accepted = await apiSendMessage(
          apiPort,
          activeSession.session_id,
          content,
          activeSession.current_agent_id,
          attachments,
          activeSessionGatewayWorkspaceId,
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => {
          const next = cloneMaps(prev);
          const pendingList =
            next.pendingConversations.get(activeSessionCacheKey) ?? [];
          writePendingList(
            next.pendingConversations,
            activeSessionCacheKey,
            pendingList.filter(
              (conversation) =>
                conversation.pendingSubmissionId !== pendingSubmissionId,
            ),
          );
          next.status = `发送失败: ${message}`;
          return next;
        });
        throw error;
      }
      const messageId = accepted.message_id;
      const jobId = accepted.job_id;

      setState((prev) => {
        const next = cloneMaps(prev);
        const conversation: ConversationView = {
          conversationId: messageId,
          sessionId: activeSession.session_id,
          userMessage: {
            message_id: messageId,
            session_id: activeSession.session_id,
            role: "user",
            content,
            metadata: {
              source: "optimistic",
              job_id: jobId,
              pending_submission_id: pendingSubmissionId,
            },
            attachments,
            created_at: submittedAt,
            updated_at: submittedAt,
          },
          assistantMessages: [],
          events: [],
          status: accepted.status === "queued" ? "queued" : "running",
          jobId,
          pending: true,
          pendingSubmissionId,
          source: "pending",
        };
        const pendingList = next.pendingConversations.get(activeSessionCacheKey) ?? [];
        next.pendingConversations.set(activeSessionCacheKey, [
          ...pendingList.filter(
            (item) => item.pendingSubmissionId !== pendingSubmissionId,
          ),
          conversation,
        ]);
        next.status =
          accepted.status === "queued" ? "已排队，等待当前任务结束" : "已发送，等待生成";
        next.contentView = prev.contentView === "agent" ? "default" : prev.contentView;
        return next;
      });
    },
    [
      activeGatewayWorkspaceId,
      apiPort,
      currentSession,
      currentSessionCacheKey,
      currentSessionGatewayWorkspaceId,
      defaultGatewayWorkspaceId,
      setState,
    ],
  );

  const compactSession = useCallback(async () => {
    const session = currentSession;
    if (!session) {
      throw new Error("当前没有可压缩上下文的会话");
    }

    setState((prev) => ({
      ...prev,
      compactLoading: true,
      status: "正在压缩上下文",
    }));

    try {
      const result = await apiCompactSessionContext(
        apiPort,
        session.session_id,
        currentSessionGatewayWorkspaceId,
      );
      setState((prev) => {
        const next = cloneMaps(prev);
        next.compactLoading = false;
        next.lastCompactResult = result;
        next.status =
          result.status === "compacted"
            ? `已压缩上下文: ${result.summarized_message_count} 条`
            : `上下文未压缩: ${result.message}`;
        appendFrontendEvent(
          next.eventQueuesBySession,
          result.session_id,
          "context_compacted",
          result.status === "compacted" ? "上下文已压缩" : "上下文未压缩",
          {
            session_id: result.session_id,
            status: result.status,
            before_message_count: result.before_message_count,
            effective_message_count_before: result.effective_message_count_before,
            effective_message_count_after: result.effective_message_count_after,
            summarized_message_count: result.summarized_message_count,
            retained_message_count: result.retained_message_count,
            history_file_path: result.history_file_path,
          },
          result.message,
          currentSessionCacheKey ?? result.session_id,
        );
        return next;
      });

      if (contentView === "agent") {
        await refreshAgentStateSnapshot(session.session_id);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((prev) => ({
        ...prev,
        compactLoading: false,
        status: `上下文压缩失败: ${message}`,
      }));
      throw error;
    }
  }, [
    apiPort,
    contentView,
    currentSession,
    currentSessionGatewayWorkspaceId,
    refreshAgentStateSnapshot,
    setState,
  ]);

  const interruptSession = useCallback(async () => {
    if (!currentSession) {
      throw new Error("当前没有可中断的会话");
    }

    setState((prev) => ({ ...prev, status: "正在中断生成..." }));
    const result = await apiInterruptSession(
      apiPort,
      currentSession.session_id,
      currentSessionGatewayWorkspaceId,
    );
    setState((prev) => ({ ...prev, status: `已中断: ${result.phase}` }));
  }, [apiPort, currentSession, currentSessionGatewayWorkspaceId, setState]);

  return {
    compactSession,
    interruptSession,
    sendMessage,
  };
}
