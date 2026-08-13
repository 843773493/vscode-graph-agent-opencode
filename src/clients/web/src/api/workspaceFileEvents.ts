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
  getApiBaseUrl,
  getGatewayToken,
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
  const localToken = await getGatewayToken(port);
  const response = await fetch(`${getApiBaseUrl(port)}/api/v1/workspace/files/events`, {
    method: "POST",
    signal: options?.signal,
    headers: {
      accept: "text/event-stream",
      "Content-Type": "application/json",
      "X-Local-Token": localToken,
      ...workspaceHeader(options?.workspaceId),
    },
    body: JSON.stringify({ paths }),
  });
  if (!response.ok || !response.body) {
    const detail = await response.clone().json().catch(() => null) as {
      detail?: string;
    } | null;
    throw new Error(
      detail?.detail
        ? `无法连接文件监听流: ${detail.detail}`
        : `无法连接文件监听流: ${response.status} ${response.statusText}`,
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
