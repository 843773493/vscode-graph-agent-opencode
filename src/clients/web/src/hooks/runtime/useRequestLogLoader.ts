import { useCallback, useEffect, useRef } from "react";
import { getLLMRequestLogs } from "../../api";
import type { SetAppState } from "../contentViewLoaderTypes";
import { errorMessage } from "../../utils/errorMessage";

export function useRequestLogLoader({
  apiPort,
  workspaceId,
  setState,
}: {
  apiPort: number;
  workspaceId: string | null;
  setState: SetAppState;
}) {
  const requestRef = useRef<AbortController | null>(null);

  const invalidateLLMRequestLogs = useCallback(() => {
    requestRef.current?.abort();
    requestRef.current = null;
  }, []);

  useEffect(() => invalidateLLMRequestLogs, [apiPort, workspaceId, invalidateLLMRequestLogs]);

  const refreshLLMRequestLogs = useCallback(
    async (sessionId: string) => {
      invalidateLLMRequestLogs();
      const controller = new AbortController();
      requestRef.current = controller;
      setState((prev) => ({
        ...prev,
        contentView: "requests",
        llmRequestLogsLoading: true,
        llmRequestLogsError: null,
        status: "正在读取 LLM 请求响应日志",
      }));

      try {
        const records = await getLLMRequestLogs(apiPort, sessionId, workspaceId, controller.signal);
        setState((prev) => {
          if (
            controller.signal.aborted ||
            prev.currentSession?.session_id !== sessionId ||
            (prev.currentSessionWorkspaceId ?? prev.activeGatewayWorkspaceId) !== workspaceId ||
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
        const message = errorMessage(error);
        setState((prev) => {
          if (
            controller.signal.aborted ||
            prev.currentSession?.session_id !== sessionId ||
            (prev.currentSessionWorkspaceId ?? prev.activeGatewayWorkspaceId) !== workspaceId ||
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
    [apiPort, workspaceId, setState, invalidateLLMRequestLogs],
  );

  return {
    invalidateLLMRequestLogs,
    refreshLLMRequestLogs,
  };
}
