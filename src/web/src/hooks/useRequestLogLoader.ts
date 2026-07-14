import { useCallback, useRef } from "react";
import { getLLMRequestLogs } from "../api";
import type { SetAppState } from "./contentViewLoaderTypes";

export function useRequestLogLoader({
  apiPort,
  workspaceId,
  setState,
}: {
  apiPort: number;
  workspaceId: string | null;
  setState: SetAppState;
}) {
  const requestIdRef = useRef(0);

  const invalidateLLMRequestLogs = useCallback(() => {
    requestIdRef.current += 1;
  }, []);

  const refreshLLMRequestLogs = useCallback(
    async (sessionId: string) => {
      const requestId = requestIdRef.current + 1;
      requestIdRef.current = requestId;
      setState((prev) => ({
        ...prev,
        contentView: "requests",
        llmRequestLogsLoading: true,
        llmRequestLogsError: null,
        status: "正在读取 LLM 请求响应日志",
      }));

      try {
        const records = await getLLMRequestLogs(apiPort, sessionId, workspaceId);
        setState((prev) => {
          if (
            requestId !== requestIdRef.current ||
            prev.currentSession?.session_id !== sessionId ||
            prev.contentView !== "requests"
          ) {
            return prev;
          }
          return {
            ...prev,
            contentView: "requests",
            llmRequestLogs: records,
            llmRequestLogsLoadedAt: new Date().toISOString(),
            llmRequestLogsLoading: false,
            llmRequestLogsError: null,
            status: `LLM 请求响应日志已加载 (${records.length} 条)`,
          };
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => {
          if (
            requestId !== requestIdRef.current ||
            prev.currentSession?.session_id !== sessionId ||
            prev.contentView !== "requests"
          ) {
            return prev;
          }
          return {
            ...prev,
            contentView: "requests",
            llmRequestLogsLoading: false,
            llmRequestLogsError: message,
            status: `LLM 请求响应日志加载失败: ${message}`,
          };
        });
      }
    },
    [apiPort, workspaceId, setState],
  );

  return {
    invalidateLLMRequestLogs,
    refreshLLMRequestLogs,
  };
}
