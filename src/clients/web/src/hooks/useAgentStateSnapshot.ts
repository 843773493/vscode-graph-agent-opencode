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

  const invalidateAgentState = useCallback(() => {
    requestIdRef.current += 1;
  }, []);

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
        const snapshot = await getAgentStateMessages(apiPort, sessionId, workspaceId);
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
    [apiPort, workspaceId, setState],
  );

  const loadAgentStateMessageRawContent = useCallback(async (
    sessionId: string,
    messageId: string,
  ): Promise<string> => {
    const snapshot = await getAgentStateMessages(apiPort, sessionId, workspaceId);
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
  }, [apiPort, workspaceId]);

  return {
    invalidateAgentState,
    loadAgentStateMessageRawContent,
    refreshAgentStateSnapshot,
  };
}
