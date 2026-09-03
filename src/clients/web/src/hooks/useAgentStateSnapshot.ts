import { useCallback, useRef } from "react";
import { getAgentStateMessages } from "../api";
import { findAgentStateMessageRawContent } from "../state/agentStateDisplay";
import type { AppState } from "../types/frontend";
import type { SetAppState } from "./contentViewLoaderTypes";

export function resetAgentStateFields(
  state: AppState,
  options: {
    loadedAt?: string | null;
    error?: string | null;
  } = {},
): AppState {
  const { loadedAt = null, error = null } = options;
  return {
    ...state,
    agentStateJsonl: "",
    agentStateMessageCount: 0,
    agentStateLoadedAt: loadedAt,
    agentStateLoading: false,
    agentStateError: error,
  };
}

export function useAgentStateSnapshotLoader({
  apiPort,
  workspaceId,
  setState,
}: {
  apiPort: number;
  workspaceId: string | null;
  setState: SetAppState;
}) {
  const requestIdRef = useRef(0);
  type AgentStateSnapshot = Awaited<ReturnType<typeof getAgentStateMessages>>;
  const snapshotRef = useRef<{
    sessionId: string;
    workspaceId: string | null;
    snapshot: AgentStateSnapshot;
  } | null>(null);
  const snapshotRequestRef = useRef<{
    key: string;
    promise: Promise<AgentStateSnapshot>;
  } | null>(null);

  const invalidateAgentState = useCallback(() => {
    requestIdRef.current += 1;
    snapshotRef.current = null;
    snapshotRequestRef.current = null;
  }, []);

  const loadSnapshot = useCallback((sessionId: string, force: boolean) => {
    const requestKey = `${workspaceId ?? ""}:${sessionId}`;
    const inFlight = snapshotRequestRef.current;
    if (inFlight?.key === requestKey) {
      return inFlight.promise;
    }
    if (
      !force
      && snapshotRef.current?.sessionId === sessionId
      && snapshotRef.current.workspaceId === workspaceId
    ) {
      return Promise.resolve(snapshotRef.current.snapshot);
    }
    const promise = getAgentStateMessages(apiPort, sessionId, workspaceId).then(
      (snapshot) => {
        snapshotRef.current = { sessionId, workspaceId, snapshot };
        return snapshot;
      },
    );
    snapshotRequestRef.current = { key: requestKey, promise };
    void promise.then(() => {
      if (snapshotRequestRef.current?.promise === promise) {
        snapshotRequestRef.current = null;
      }
    }, () => {
      if (snapshotRequestRef.current?.promise === promise) {
        snapshotRequestRef.current = null;
      }
    });
    return promise;
  }, [apiPort, workspaceId]);

  const refreshAgentStateSnapshot = useCallback(
    async (sessionId: string) => {
      const requestId = requestIdRef.current + 1;
      requestIdRef.current = requestId;
      setState((prev) => ({
        ...prev,
        contentView: "agent",
        agentStateLoading: true,
        agentStateError: null,
        status: "正在读取上下文状态",
      }));

      try {
        const snapshot = await loadSnapshot(sessionId, true);
        setState((prev) => {
          if (
            requestId !== requestIdRef.current ||
            prev.currentSession?.session_id !== sessionId ||
            prev.contentView !== "agent"
          ) {
            return prev;
          }
          return {
            ...prev,
            contentView: "agent",
            agentStateJsonl: snapshot.jsonl,
            agentStateMessageCount: snapshot.message_count,
            agentStateLoadedAt: new Date().toISOString(),
            agentStateLoading: false,
            agentStateError: null,
            status: `上下文状态已加载 (${snapshot.message_count} 条消息)`,
          };
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => {
          if (
            requestId !== requestIdRef.current ||
            prev.currentSession?.session_id !== sessionId ||
            prev.contentView !== "agent"
          ) {
            return prev;
          }
          return {
            ...prev,
            contentView: "agent",
            agentStateLoading: false,
            agentStateError: message,
            status: `上下文状态加载失败: ${message}`,
          };
        });
      }
    },
    [loadSnapshot, setState],
  );

  const loadAgentStateMessageRawContent = useCallback(async (
    sessionId: string,
    messageId: string,
  ): Promise<string> => {
    const snapshot = await loadSnapshot(sessionId, false);
    if (snapshot.session_id !== sessionId) {
      throw new Error(
        `Agent State 会话不匹配: 请求 ${sessionId}，响应 ${snapshot.session_id}`,
      );
    }
    const content = findAgentStateMessageRawContent(snapshot.jsonl, messageId);
    if (content === null) {
      throw new Error(`Agent State 中找不到消息原文: message_id=${messageId}`);
    }
    return content;
  }, [loadSnapshot]);

  return {
    invalidateAgentState,
    loadAgentStateMessageRawContent,
    refreshAgentStateSnapshot,
  };
}
