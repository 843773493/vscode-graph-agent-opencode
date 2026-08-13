import { useEffect } from "react";
import type { ConversationContentView } from "../types/frontend";
import type { RefreshOptions } from "./contentViewLoaderTypes";

export function useContentViewEffects({
  contentView,
  sessionId,
  refreshLLMRequestLogs,
  refreshSessionChanges,
  refreshSessionResources,
}: {
  contentView: ConversationContentView;
  sessionId: string | null;
  refreshLLMRequestLogs: (sessionId: string) => Promise<void>;
  refreshSessionChanges: (sessionId: string) => Promise<void>;
  refreshSessionResources: (
    sessionId: string,
    options?: RefreshOptions,
  ) => Promise<void>;
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

  useEffect(() => {
    if (contentView !== "resources" || !sessionId) {
      return;
    }

    let disposed = false;
    let pollInFlight = false;
    const poll = async (silent: boolean) => {
      if (disposed || pollInFlight || document.visibilityState !== "visible") {
        return;
      }
      pollInFlight = true;
      try {
        await refreshSessionResources(sessionId, { silent });
      } finally {
        pollInFlight = false;
      }
    };

    const initialTimerId = window.setTimeout(() => {
      void poll(false);
    }, 120);
    const timerId = window.setInterval(() => {
      void poll(true);
    }, 5000);

    return () => {
      disposed = true;
      window.clearTimeout(initialTimerId);
      window.clearInterval(timerId);
    };
  }, [
    contentView,
    refreshSessionResources,
    sessionId,
  ]);
}
