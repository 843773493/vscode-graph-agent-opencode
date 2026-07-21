import { useEffect } from "react";
import { listMessages } from "../api";
import { listPendingRequests } from "../pendingRequestsApi";
import { cloneMaps } from "../state/appStateMaps";
import { updateAttachmentSummariesFromMessages } from "../state/attachments";
import { appendFrontendEvent } from "../state/traceEvents";
import { writePendingSnapshot } from "../state/conversations";
import type { SetAppState } from "./contentViewLoaderTypes";

export function useSessionHistoryLoader({
  apiPort,
  sessionId,
  workspaceId,
  sessionCacheKey,
  reloadNonce,
  setState,
}: {
  apiPort: number | null;
  sessionId: string | null;
  workspaceId: string | null;
  sessionCacheKey: string | null;
  reloadNonce: number;
  setState: SetAppState;
}) {
  useEffect(() => {
    if (!apiPort || !sessionId) return;
    const targetWorkspaceId = workspaceId;
    const targetSessionCacheKey = sessionCacheKey ?? sessionId;

    let cancelled = false;
    setState((prev) => {
      if (prev.currentSession?.session_id !== sessionId) return prev;
      const next = cloneMaps(prev);
      appendFrontendEvent(
        next.eventQueuesBySession,
        sessionId,
        "session_load_started",
        "开始加载会话历史",
        { session_id: sessionId },
        "",
        targetSessionCacheKey,
      );
      return next;
    });

    const timerId = window.setTimeout(() => void (async () => {
      try {
        const [messages, pendingSnapshot] = await Promise.all([
          listMessages(apiPort, sessionId, targetWorkspaceId),
          listPendingRequests(apiPort, sessionId, targetWorkspaceId),
        ]);
        if (cancelled) return;
        setState((prev) => {
          if (prev.currentSession?.session_id !== sessionId) return prev;
          if (
            targetWorkspaceId &&
            prev.currentSessionWorkspaceId !== targetWorkspaceId
          ) {
            return prev;
          }
          const next = cloneMaps(prev);
          next.messages = messages.items ?? [];
          writePendingSnapshot(
            next.pendingConversations,
            next.activeJobIdsBySession,
            pendingSnapshot,
            targetSessionCacheKey,
          );
          updateAttachmentSummariesFromMessages(
            next.sessionAttachmentSummaries,
            next.messages,
          );
          appendFrontendEvent(
            next.eventQueuesBySession,
            sessionId,
            "session_load_completed",
            "会话历史加载完成",
            {
              session_id: sessionId,
              message_count: messages.items?.length ?? 0,
            },
            "",
            targetSessionCacheKey,
          );
          next.status = "会话历史加载完成";
          return next;
        });
      } catch (error) {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => {
          if (prev.currentSession?.session_id !== sessionId) {
            return prev;
          }
          const next = cloneMaps(prev);
          next.status = `加载失败: ${message}`;
          appendFrontendEvent(
            next.eventQueuesBySession,
            sessionId,
            "session_load_failed",
            "会话历史加载失败",
            { session_id: sessionId, error: message },
            message,
            targetSessionCacheKey,
          );
          return next;
        });
      }
    })(), 120);

    return () => {
      cancelled = true;
      window.clearTimeout(timerId);
    };
  }, [
    apiPort,
    reloadNonce,
    sessionCacheKey,
    sessionId,
    setState,
    workspaceId,
  ]);
}
