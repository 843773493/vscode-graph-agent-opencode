import { useCallback, useRef, type Dispatch, type SetStateAction } from "react";
import { getAgentStateMessages } from "../api";
import type { Session } from "../types/backend";
import type { AppState, ConversationContentView } from "../types/frontend";

type SetAppState = Dispatch<SetStateAction<AppState>>;

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

export function useAgentStateLoader({
  apiPort,
  currentSession,
  setState,
}: {
  apiPort: number;
  currentSession: Session | null;
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
        status: "正在读取 Agent State 快照",
      }));

      try {
        const snapshot = await getAgentStateMessages(apiPort, sessionId);
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
            status: `Agent State 快照已加载 (${snapshot.message_count} 条消息)`,
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
            status: `Agent State 加载失败: ${message}`,
          };
        });
      }
    },
    [apiPort, setState],
  );

  const switchContentView = useCallback(
    async (view: ConversationContentView) => {
      if (view === "default") {
        invalidateAgentState();
        setState((prev) => ({
          ...prev,
          contentView: "default",
          agentStateLoading: false,
          agentStateError: null,
          status: "默认视图",
        }));
        return;
      }

      if (view === "events") {
        invalidateAgentState();
        setState((prev) => ({
          ...prev,
          contentView: "events",
          agentStateLoading: false,
          agentStateError: null,
          status: "事件视图",
        }));
        return;
      }

      if (!currentSession) {
        invalidateAgentState();
        setState((prev) => ({
          ...resetAgentStateFields(prev, {
            loadedAt: new Date().toISOString(),
            error: "当前没有会话可读取 Agent State",
          }),
          contentView: "agent",
          status: "没有会话可读取 Agent State",
        }));
        return;
      }

      await refreshAgentStateSnapshot(currentSession.session_id);
    },
    [
      currentSession,
      invalidateAgentState,
      refreshAgentStateSnapshot,
      setState,
    ],
  );

  return {
    invalidateAgentState,
    refreshAgentStateSnapshot,
    switchContentView,
  };
}
