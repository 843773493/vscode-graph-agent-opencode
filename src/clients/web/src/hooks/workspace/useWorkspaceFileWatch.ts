import { useEffect, useRef } from "react";
import {
  registerWorkspaceObservation,
  useWorkspaceObservationState,
} from "./workspaceObservationLifecycle";

import {
  streamWorkspaceFileEvents,
} from "../../api";
import type { WorkspaceFileStreamBatch } from "../../types/backend";
import { dispatchWorkspaceFileChanges } from "../../state/workspaceFileTreeEvents";
import { errorMessage } from "../../utils/errorMessage";
import { SESSION_STREAM_MAX_RECONNECT_ATTEMPTS } from "../sessionEventStream/sessionEventStreamPolicy";

interface UseWorkspaceFileWatchOptions {
  active: boolean;
  port: number;
  workspaceId: string | null;
  paths: readonly string[];
  onOverflow: (roots: readonly string[]) => void;
  onStatusChange: (message: string) => void;
}

const MAX_RECONNECT_DELAY_MS = 10_000;

export function useWorkspaceFileWatch({
  active,
  port,
  workspaceId,
  paths,
  onOverflow,
  onStatusChange,
}: UseWorkspaceFileWatchOptions): void {
  const observationState = useWorkspaceObservationState(workspaceId);
  const pathKey = JSON.stringify([...new Set(paths)].sort());
  const cursor = useRef<{ scope: string; eventId: number | undefined }>({ scope: "", eventId: undefined });

  useEffect(() => {
    if (!active || observationState.paused) {
      return;
    }
    const controller = new AbortController();
    const unregisterObservation = registerWorkspaceObservation(workspaceId, controller);
    const watchPaths = JSON.parse(pathKey) as string[];
    const cursorScope = `${port}:${workspaceId}:${pathKey}`;
    if (cursor.current.scope !== cursorScope) cursor.current = { scope: cursorScope, eventId: undefined };
    let reconnectDelayMs = 500;
    let reconnectAttempt = 0;
    let reconnectTimer: number | null = null;

    const handleBatch = (batch: WorkspaceFileStreamBatch) => {
      reconnectDelayMs = 500;
      cursor.current.eventId = Math.max(cursor.current.eventId ?? 0, batch.event_id);
      if (batch.overflow) {
        onOverflow(batch.invalidated_roots);
        onStatusChange("文件变化过快，已重新同步展开目录");
        return;
      }
      dispatchWorkspaceFileChanges(workspaceId, batch.changes, batch.directories, batch.snapshots);
    };

    const connect = async (): Promise<void> => {
      if (controller.signal.aborted) return;
      try {
        await streamWorkspaceFileEvents(port, watchPaths, {
          workspaceId,
          afterEventId: cursor.current.eventId,
          signal: controller.signal,
          onBatch: handleBatch,
          onConnected: () => {
            // 只有真的从断流里接回来才写恢复文案：计数归零之后不再宣称
            // 「正在重连」，否则状态栏会永久停留在一条与事实不符的失败文案上。
            if (reconnectAttempt > 0) {
              onStatusChange("文件监听流已恢复实时同步");
            }
            reconnectDelayMs = 500;
            reconnectAttempt = 0;
          },
        });
        if (!controller.signal.aborted) {
          throw new Error("文件监听流意外结束");
        }
      } catch (error) {
        if (controller.signal.aborted) {
          return;
        }
        // 有界重连：连续到达上限后停止重连并给出可见终态，不再无限刷新
        // 「正在重连」文案。onConnected 会在连接真正建立时把计数归零，因此
        // 这里限制的是「连续若干次都没能建立连接」。
        if (reconnectAttempt >= SESSION_STREAM_MAX_RECONNECT_ATTEMPTS) {
          onStatusChange(
            `文件监听流连续 ${SESSION_STREAM_MAX_RECONNECT_ATTEMPTS} 次重连失败，已停止自动重连；请刷新页面或切换工作区后重试`,
          );
          return;
        }
        reconnectAttempt += 1;
        const message = errorMessage(error);
        onStatusChange(`${message}；正在重连`);
        reconnectTimer = window.setTimeout(() => {
          reconnectTimer = null;
          void connect();
        }, reconnectDelayMs);
        reconnectDelayMs = Math.min(reconnectDelayMs * 2, MAX_RECONNECT_DELAY_MS);
      }
    };

    void connect();
    return () => {
      unregisterObservation();
      controller.abort();
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
      }
    };
  }, [active, observationState, onOverflow, onStatusChange, pathKey, port, workspaceId]);
}
