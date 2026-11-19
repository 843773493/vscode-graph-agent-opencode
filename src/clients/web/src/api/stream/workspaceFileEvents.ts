import type { WorkspaceFileStreamBatch } from "../../types/backend";
import {
  decodeJsonSseData,
  defineSseEvent,
} from "../../sse/sseClient";
import { SSE_IDLE_TIMEOUT_MS } from "../../sse/sseIdleTimeout";
import {
  validateSseError,
  validateWorkspaceFileChangeBatch,
} from "../../sse/sseRuntimeSchemas";
import { workspaceHeader } from "../http";
import { consumeRealtimeSubscription } from "./realtime";

export async function streamWorkspaceFileEvents(
  port: number,
  paths: readonly string[],
  options?: {
    workspaceId?: string | null;
    onBatch?: (batch: WorkspaceFileStreamBatch) => void;
    onConnected?: () => void;
    signal?: AbortSignal;
    afterEventId?: number;
  },
): Promise<void> {
  let afterEventId = options?.afterEventId;
  await consumeRealtimeSubscription(port, () => ({ path: '/api/v1/workspace/files/events', method: 'POST', headers: workspaceHeader(options?.workspaceId), body: JSON.stringify({ paths: [...paths], after_event_id: afterEventId }) }), {
    signal: options?.signal,
    idleTimeoutMs: SSE_IDLE_TIMEOUT_MS,
    idleTimeoutError: (timeoutMs) => new Error(
      `文件监听流超过 ${timeoutMs}ms 未收到任何数据`,
    ),
    onConnected: (headers) => {
      const cursor = headers.get("X-Workspace-File-Cursor");
      const value = Number(cursor);
      if (cursor === null || !Number.isSafeInteger(value) || value < 0) {
        throw new Error(`文件观察者连接缺少有效持久游标: ${cursor}`);
      }
      afterEventId = value;
      options?.onConnected?.();
    },
    events: {
      changes: defineSseEvent(
        (data, frame) => validateWorkspaceFileChangeBatch(
          decodeJsonSseData(data, frame),
        ),
        (batch) => {
          options?.onBatch?.(batch);
          afterEventId = Math.max(afterEventId ?? 0, batch.event_id);
        },
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
