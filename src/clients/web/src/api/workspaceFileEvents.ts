import type { WorkspaceFileStreamBatch } from "../types/backend";
import {
  consumeSseResponse,
  decodeJsonSseData,
  defineSseEvent,
} from "../sseClient";
import {
  validateSseError,
  validateWorkspaceFileChangeBatch,
} from "../sseRuntimeSchemas";
import {
  requestGatewayResponse,
  workspaceHeader,
} from "./http";

export async function streamWorkspaceFileEvents(
  port: number,
  paths: readonly string[],
  options?: {
    workspaceId?: string | null;
    onBatch?: (batch: WorkspaceFileStreamBatch) => void;
    onConnected?: () => void;
    signal?: AbortSignal;
  },
): Promise<void> {
  // SSE 长期连接只共享统一凭据与刷新重试；生命周期内的断线重连由调用方负责。
  const response = await requestGatewayResponse(
    port,
    "/api/v1/workspace/files/events",
    {
      method: "POST",
      signal: options?.signal,
      skipGatewayUserSession: true,
      headers: {
        accept: "text/event-stream",
        "Content-Type": "application/json",
        ...workspaceHeader(options?.workspaceId),
      },
      body: JSON.stringify({ paths }),
    },
  );
  if (!response.body) {
    throw new Error(
      `无法连接文件监听流: ${response.status} ${response.statusText}`,
    );
  }
  options?.onConnected?.();
  await consumeSseResponse(response, {
    signal: options?.signal,
    idleTimeoutMs: 45_000,
    idleTimeoutError: (timeoutMs) => new Error(
      `文件监听流超过 ${timeoutMs}ms 未收到任何数据`,
    ),
    events: {
      changes: defineSseEvent(
        (data, frame) => validateWorkspaceFileChangeBatch(
          decodeJsonSseData(data, frame),
        ),
        (batch) => options?.onBatch?.(batch),
      ),
      error: defineSseEvent(
        (data, frame) => validateSseError(decodeJsonSseData(data, frame)),
        (error) => {
          throw new Error(error.message);
        },
      ),
    },
  });
}
