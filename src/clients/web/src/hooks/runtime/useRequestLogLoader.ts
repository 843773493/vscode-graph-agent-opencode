import { useCallback, useEffect, useRef, useState } from "react";
import { getLLMRequestLogs } from "../../api";
import { getLLMRequestLog } from "../../api/session/sessionMessages";
import type { LLMRequestLogRecord, LLMRequestLogSummaryDTO } from "../../types/backend";
import type { SetAppState } from "../contentViewLoaderTypes";
import { buildRequestLogKeyFlow, type RequestLogKeyFlow } from "../../state/requestLogDisplay";
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

/** 单条正文只在展开期间持有；隐藏面板、折叠或切换 owner 时取消读取。 */
export function useRequestLogDetail(apiPort: number, workspaceId: string | null,
  sessionId: string, fileName: string, active: boolean) {
  const [state, setState] = useState<{ log?: LLMRequestLogRecord; error?: string }>({});
  useEffect(() => {
    setState({});
    if (!active) return;
    const controller = new AbortController();
    void getLLMRequestLog(apiPort, sessionId, fileName, workspaceId, controller.signal).then(
      (log) => { if (!controller.signal.aborted) setState({ log }); },
      (error: unknown) => { if (!controller.signal.aborted) setState({ error: errorMessage(error) }); },
    );
    return () => controller.abort();
  }, [apiPort, workspaceId, sessionId, fileName, active]);
  return state;
}

/** 完整请求链路按用户操作读取，不将正文长期保存在列表状态中。 */
export function useRequestLogChain(logs: LLMRequestLogSummaryDTO[], apiPort: number,
  workspaceId: string | null, active: boolean) {
  const [requestVersion, setRequestVersion] = useState(0);
  const [flow, setFlow] = useState<RequestLogKeyFlow>();
  const [error, setError] = useState<string>();
  useEffect(() => {
    setFlow(undefined);
    setError(undefined);
    if (!active) { setRequestVersion(0); return; }
    if (!requestVersion) return;
    const controller = new AbortController();
    const load = async () => {
      // 全链路是显式操作；顺序读取，避免同时传输多份大正文。
      const records: LLMRequestLogRecord[] = [];
      for (const summary of logs) {
        if (controller.signal.aborted) return;
        records.push(await getLLMRequestLog(apiPort, summary.session_id, summary.file_name, workspaceId, controller.signal));
      }
      if (!controller.signal.aborted) setFlow(buildRequestLogKeyFlow(records));
    };
    void load().catch((error: unknown) => {
      if (!controller.signal.aborted) setError(errorMessage(error));
    });
    return () => controller.abort();
  }, [requestVersion, active, logs, apiPort, workspaceId]);
  return { loading: requestVersion > 0 && !flow && !error,
    load: () => setRequestVersion((version) => version + 1), flow, error };
}
