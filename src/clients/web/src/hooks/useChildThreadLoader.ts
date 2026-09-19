import { useCallback, useEffect, useRef, useState } from "react";
import { listChildThreads } from "../api";
import type { ChildThreadSummary } from "../types/backend";

export interface ChildThreadSnapshot {
  threads: ChildThreadSummary[];
  /** 后端返回的 total；列表被截断时可用于提示“后端共 N 条”。 */
  total: number;
  loading: boolean;
  error: string | null;
  loadedAt: string | null;
}

const EMPTY_SNAPSHOT: ChildThreadSnapshot = {
  threads: [],
  total: 0,
  loading: false,
  error: null,
  loadedAt: null,
};

/**
 * 当前会话 child thread 列表的加载器。
 * 会话/工作区变化时清空并重新加载；请求失败时保留旧数据并透出错误，
 * 列表数据始终以后端返回为准，不做本地伪造。
 */
export function useChildThreadLoader({
  apiPort,
  workspaceId,
  sessionId,
  enabled,
}: {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
  /** 右侧侧边栏对应标签可见时才加载与轮询。 */
  enabled: boolean;
}): ChildThreadSnapshot & {
  refresh: (options?: { silent?: boolean }) => Promise<void>;
} {
  const [snapshot, setSnapshot] = useState<ChildThreadSnapshot>(EMPTY_SNAPSHOT);
  const requestIdRef = useRef(0);

  const refresh = useCallback(
    async (options: { silent?: boolean } = {}) => {
      if (!sessionId) {
        return;
      }
      const silent = options.silent === true;
      const requestId = requestIdRef.current + 1;
      requestIdRef.current = requestId;

      setSnapshot((prev) => ({
        ...prev,
        loading: silent ? prev.loading : true,
        error: null,
      }));

      try {
        const result = await listChildThreads(apiPort, sessionId, workspaceId);
        // 只有仍然最新的请求才能落地；会话已切换时丢弃过期响应。
        if (requestIdRef.current !== requestId) {
          return;
        }
        setSnapshot({
          threads: result.items,
          total: result.total,
          loading: false,
          error: null,
          loadedAt: new Date().toISOString(),
        });
      } catch (error: unknown) {
        if (requestIdRef.current !== requestId) {
          return;
        }
        // 失败时保留已有数据用于展示，错误透明呈现，等待下次刷新重新获取。
        setSnapshot((prev) => ({
          ...prev,
          loading: false,
          error: error instanceof Error ? error.message : String(error),
        }));
      }
    },
    [apiPort, sessionId, workspaceId],
  );

  // 会话或可见性变化：使在途请求失效，清空旧会话数据并按需重新加载。
  useEffect(() => {
    requestIdRef.current += 1;
    if (!enabled || !sessionId) {
      setSnapshot(EMPTY_SNAPSHOT);
      return;
    }
    void refresh();
  }, [enabled, refresh]);

  return {
    ...snapshot,
    refresh,
  };
}
