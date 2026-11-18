import type { ConversationView } from "../types/frontend";
import type { SessionTurnTimeline } from "./session/turnTimeline";

export interface ThreadStatusInput {
  conversations: readonly ConversationView[];
  timeline: SessionTurnTimeline | null;
  activeJobId: string | null;
  active: boolean;
  syncError?: string | null;
}

/** 只统计后端已接受的队列；提交结果未知不能冒充已排队。 */
export function queuedThreadMessages(conversations: readonly ConversationView[]): ConversationView[] {
  return conversations.filter(item => item.pending && item.status === "queued" && item.jobId
    && !item.activeJobOverlay && item.userMessage && item.deliveryPolicy)
    .sort((a, b) => (a.pendingPosition ?? Number.MAX_SAFE_INTEGER) - (b.pendingPosition ?? Number.MAX_SAFE_INTEGER));
}

/** 原始同步错误交给输出；已完成轮次的旧连接状态不产生新错误。 */
export function threadStatusSyncError({ conversations, timeline, activeJobId, syncError }: ThreadStatusInput): string | null {
  const historyError = syncError || timeline?.error;
  if (historyError) return historyError;
  const current = activeJobId ? conversations.find(item => item.jobId === activeJobId)
    : [...conversations].reverse().find(item => item.jobId && item.status !== "queued");
  const stream = current?.messageStream;
  const terminal = Boolean(stream && stream.streamStatus !== "open" && stream.streamStatus !== "interrupting")
    || current?.status === "done" || current?.status === "error" && !current.pending;
  return !terminal && stream && ["disconnected", "gap", "retry_exhausted"].includes(stream.connectionStatus)
    ? stream.protocolError ?? null : null;
}
