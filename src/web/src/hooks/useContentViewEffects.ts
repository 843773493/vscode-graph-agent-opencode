import { useEffect } from "react";
import type { ConversationContentView } from "../types/frontend";
import type { RefreshOptions } from "./contentViewLoaderTypes";

export function useContentViewEffects({
  contentView,
  sessionId,
  refreshLLMRequestLogs,
  refreshSessionResources,
}: {
  contentView: ConversationContentView;
  sessionId: string | null;
  refreshLLMRequestLogs: (sessionId: string) => Promise<void>;
  refreshSessionResources: (
    sessionId: string,
    options?: RefreshOptions,
  ) => Promise<void>;
}) {
  useEffect(() => {
    if (contentView !== "requests" || !sessionId) {
      return;
    }

    void refreshLLMRequestLogs(sessionId);
  }, [
    contentView,
    refreshLLMRequestLogs,
    sessionId,
  ]);

  useEffect(() => {
    if (contentView !== "resources" || !sessionId) {
      return;
    }

    void refreshSessionResources(sessionId);
    const timerId = window.setInterval(() => {
      void refreshSessionResources(sessionId, { silent: true });
    }, 2000);

    return () => {
      window.clearInterval(timerId);
    };
  }, [
    contentView,
    refreshSessionResources,
    sessionId,
  ]);
}
