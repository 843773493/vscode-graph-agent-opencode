import { useCallback } from "react";
import {
  compactSessionContext as apiCompactSessionContext,
  interruptSession as apiInterruptSession,
  sendUserMessage as apiSendMessage,
} from "../api";
import type { AttachmentRef, MessageRunAccepted, Session } from "../types/backend";
import type { ConversationContentView, ConversationView } from "../types/frontend";
import { cloneMaps } from "../state/appStateMaps";
import { updateSessionAttachmentSummary } from "../state/attachments";
import { writePendingList } from "../state/conversations";
import { appendFrontendEvent } from "../state/traceEvents";
import type { SetAppState } from "./contentViewLoaderTypes";

export function useSessionRunActions({
  apiPort,
  currentSession,
  contentView,
  setState,
  refreshAgentStateSnapshot,
}: {
  apiPort: number;
  currentSession: Session | null;
  contentView: ConversationContentView;
  setState: SetAppState;
  refreshAgentStateSnapshot: (sessionId: string) => Promise<void>;
}) {
  const sendMessage = useCallback(
    async (content: string, attachments: AttachmentRef[] = []) => {
      if (!currentSession) {
        throw new Error("当前没有可发送消息的会话");
      }

      const session = currentSession;
      const pendingSubmissionId = `pending_submission_${Date.now()}`;
      const submittedAt = new Date().toISOString();
      setState((prev) => {
        const next = cloneMaps(prev);
        const conversation: ConversationView = {
          conversationId: pendingSubmissionId,
          sessionId: session.session_id,
          userMessage: {
            message_id: pendingSubmissionId,
            session_id: session.session_id,
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
          events: [],
          status: "running",
          jobId: null,
          pending: true,
          pendingSubmissionId,
          source: "pending",
        };
        updateSessionAttachmentSummary(
          next.sessionAttachmentSummaries,
          session.session_id,
          attachments,
          submittedAt,
        );
        const pendingList =
          next.pendingConversations.get(session.session_id) ?? [];
        next.pendingConversations.set(session.session_id, [
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
          session.session_id,
          content,
          session.current_agent_id,
          attachments,
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => {
          const next = cloneMaps(prev);
          const pendingList =
            next.pendingConversations.get(session.session_id) ?? [];
          writePendingList(
            next.pendingConversations,
            session.session_id,
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
      const messageId = accepted.message_id ?? `local_user_${Date.now()}`;
      const jobId = accepted.job_id ?? null;

      setState((prev) => {
        const next = cloneMaps(prev);
        const conversation: ConversationView = {
          conversationId: messageId,
          sessionId: session.session_id,
          userMessage: {
            message_id: messageId,
            session_id: session.session_id,
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
          events: [],
          status: accepted.status === "queued" ? "queued" : "running",
          jobId,
          pending: true,
          pendingSubmissionId,
          source: "pending",
        };
        const pendingList = next.pendingConversations.get(session.session_id) ?? [];
        next.pendingConversations.set(session.session_id, [
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
    [apiPort, currentSession, setState],
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
      const result = await apiCompactSessionContext(apiPort, session.session_id);
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
    refreshAgentStateSnapshot,
    setState,
  ]);

  const interruptSession = useCallback(async () => {
    if (!currentSession) {
      throw new Error("当前没有可中断的会话");
    }

    setState((prev) => ({ ...prev, status: "正在中断生成..." }));
    const result = await apiInterruptSession(apiPort, currentSession.session_id);
    setState((prev) => ({ ...prev, status: `已中断: ${result.phase}` }));
  }, [apiPort, currentSession, setState]);

  return {
    compactSession,
    interruptSession,
    sendMessage,
  };
}
