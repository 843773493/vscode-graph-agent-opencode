import { useEffect } from "react";
import type { ConversationContentView } from "../types/frontend";

export function useContentViewEffects({
  contentView,
  sessionId,
  refreshLLMRequestLogs,
  refreshSessionChanges,
}: {
  contentView: ConversationContentView;
  sessionId: string | null;
  refreshLLMRequestLogs: (sessionId: string) => Promise<void>;
  refreshSessionChanges: (sessionId: string) => Promise<void>;
}) {
  useEffect(() => {
    if (contentView !== "requests" || !sessionId) {
      return;
    }

    const timerId = window.setTimeout(() => {
      void refreshLLMRequestLogs(sessionId);
    }, 120);
    return () => window.clearTimeout(timerId);
  }, [
    contentView,
    refreshLLMRequestLogs,
    sessionId,
  ]);

  useEffect(() => {
    if (contentView !== "changes" || !sessionId) {
      return;
    }

    const timerId = window.setTimeout(() => {
      void refreshSessionChanges(sessionId);
    }, 120);
    return () => window.clearTimeout(timerId);
  }, [
    contentView,
    refreshSessionChanges,
    sessionId,
  ]);
}
