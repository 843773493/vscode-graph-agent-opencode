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
import { consumeWorkspaceObservation } from "./workspaceObservation";

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
  await consumeWorkspaceObservation(port, workspaceHeader(options?.workspaceId), () => ({
    kind: "workspace_files", paths: [...paths], files_after: afterEventId,
  }), {
    signal: options?.signal,
    idleTimeoutMs: SSE_IDLE_TIMEOUT_MS,
    idleTimeoutError: (timeoutMs) => new Error(
      `文件监听流超过 ${timeoutMs}ms 未收到任何数据`,
    ),
    onConnected: (_routeRevision, cursor) => {
      const value = Number(cursor);
      if (cursor === undefined || !Number.isSafeInteger(value) || value < 0) {
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
