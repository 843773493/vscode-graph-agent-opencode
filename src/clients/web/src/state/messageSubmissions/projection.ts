import type { MessageRunAccepted } from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import { dispatchToConversationProjection } from "../conversations/pendingQueue";
import type { FrozenMessageSubmission } from "./storage";

/** 本地提交只显示接收等待；取得回执后完全采用原 Job 的调度事实。 */
export function submissionConversation(record: FrozenMessageSubmission, accepted: MessageRunAccepted | null): ConversationView {
  const { request, owner, createdAt } = record;
  const operationId = request.operation_id;
  const terminal = accepted && ["completed", "failed", "cancelled", "timed_out"].includes(accepted.status);
  return {
    conversationId: accepted?.message_id ?? operationId, displayMode: "live", sessionId: owner.sessionId, threadId: owner.threadId,
    userMessage: {
      message_id: accepted?.message_id ?? operationId, session_id: owner.sessionId, thread_id: owner.threadId,
      role: "user", content: request.message.content, attachments: request.message.attachments ?? [],
      metadata: { ...request.message.metadata, source: accepted ? "optimistic" : "submission",
        pending_submission_id: operationId, ...(accepted ? { job_id: accepted.job_id } : {}),
        ...(record.error ? { submission_error: record.error } : {}) },
      created_at: createdAt, updated_at: createdAt,
    },
    assistantMessages: [], events: [], jobId: accepted?.job_id ?? null, turnId: accepted?.turn_id ?? undefined,
    status: accepted ? accepted.status === "queued" ? "queued" : accepted.status === "completed" ? "done" : terminal ? "error" : "running"
      : record.state === "rejected" ? "error" : "queued",
    pending: !terminal && record.state !== "rejected", activeJobOverlay: Boolean(accepted && !terminal && accepted.status !== "queued"),
    pendingSubmissionId: operationId, source: "pending",
    ...(accepted ? dispatchToConversationProjection(accepted.dispatch) : {}),
  };
}

export function mergeSubmissionConversation(existing: ConversationView[], conversation: ConversationView) {
  const live = existing.find(item => conversation.jobId ? item.jobId === conversation.jobId : item.pendingSubmissionId === conversation.pendingSubmissionId);
  if (live?.jobId && !conversation.jobId) return existing;
  const keepLive = live && (Boolean(live.turnStatus) || live.status === "done"
    || (live.status === "error" && !live.pending && Boolean(live.jobId))
    || (live.status === "running" && conversation.status === "queued")
    || (live.queueSnapshotVersion ?? -1) > (conversation.queueSnapshotVersion ?? -1));
  const merged = live ? { ...conversation, ...live, ...(!keepLive ? conversation : {}), conversationId: conversation.conversationId,
    userMessage: conversation.userMessage, jobId: conversation.jobId, turnId: conversation.turnId ?? live.turnId,
    assistantMessages: live.assistantMessages, events: live.events,
  } : conversation;
  return [...existing.filter(item => item.pendingSubmissionId !== conversation.pendingSubmissionId
    && (!conversation.jobId || item.jobId !== conversation.jobId)), merged];
}
