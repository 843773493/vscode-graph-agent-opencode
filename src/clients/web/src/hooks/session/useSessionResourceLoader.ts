import { useCallback, useEffect, useRef } from "react";
import {
  controlSessionResource as apiControlSessionResource,
  getSessionResources,
} from "../../api";
import type {
  Session,
  SessionResourceAction,
  SessionResourceKind,
} from "../../types/backend";
import {
  actionLabelForKind,
  resourceActionStatusLabel,
  statusLabel,
} from "../../state/display/resourceDisplay";
import type { RefreshOptions, SetAppState } from "../contentViewLoaderTypes";
import { errorMessage } from "../../utils/errorMessage";

export type SessionResourceRefreshResult = "loaded" | "failed" | "stale";

/** 只为可见资源面板轮询；两个工作台共用同一调度和权威读取链。 */
export function useSessionResourcePolling({ active, workspaceId, sessionId, refresh, onStatusChange }: {
  active: boolean;
  workspaceId: string | null;
  sessionId: string | null;
  refresh: (sessionId: string, options?: RefreshOptions) => Promise<SessionResourceRefreshResult>;
  onStatusChange: (message: string) => void;
}): void {
  useEffect(() => {
    if (!active || !sessionId) return;
    let disposed = false;
    let pollInFlight = false;
    const poll = async (silent: boolean) => {
      if (disposed || pollInFlight || (silent && document.visibilityState !== "visible")) return;
      pollInFlight = true;
      try {
        const result = await refresh(sessionId, { silent });
        if (result === "failed" && !silent) onStatusChange("后台连接刷新失败，请查看运行与连接中的错误详情");
      } finally { pollInFlight = false; }
    };
    const initialTimer = window.setTimeout(() => void poll(false), 120);
    const timer = window.setInterval(() => void poll(true), 5000);
    return () => {
      disposed = true;
      window.clearTimeout(initialTimer);
      window.clearInterval(timer);
    };
  }, [active, workspaceId, sessionId, refresh, onStatusChange]);
}


export function useSessionResourceLoader({
  apiPort,
  currentSession,
  workspaceId,
  setState,
}: {
  apiPort: number;
  currentSession: Session | null;
  workspaceId: string | null;
  setState: SetAppState;
}) {
  const requestIdRef = useRef(0);
  const ownerRef = useRef({ apiPort, workspaceId, sessionId: currentSession?.session_id });
  const inFlightRefreshRef = useRef<{
    key: string;
    promise: Promise<SessionResourceRefreshResult>;
  } | null>(null);

  if (
    ownerRef.current.apiPort !== apiPort
    || ownerRef.current.workspaceId !== workspaceId
    || ownerRef.current.sessionId !== currentSession?.session_id
  ) {
    ownerRef.current = { apiPort, workspaceId, sessionId: currentSession?.session_id };
    requestIdRef.current += 1;
    inFlightRefreshRef.current = null;
  }

  const invalidateSessionResources = useCallback(() => {
    ownerRef.current = { ...ownerRef.current };
    requestIdRef.current += 1;
    inFlightRefreshRef.current = null;
  }, []);

  const readSessionResources = useCallback(
    async (sessionId: string, options: RefreshOptions, failureStatus: string | null) => {
      const owner = ownerRef.current;
      if (owner.sessionId !== sessionId || owner.apiPort !== apiPort || owner.workspaceId !== workspaceId) return "stale" as const;
      const requestKey = `${workspaceId ?? ""}:${sessionId}`;
      const existing = inFlightRefreshRef.current;
      // 控制失败必须新读 owner，不能复用动作之前已经在途的旧结果。
      if (failureStatus === null && !options.force && existing?.key === requestKey) {
        return existing.promise;
      }
      const silent = options.silent === true;
      const requestId = requestIdRef.current + 1;
      requestIdRef.current = requestId;

      const request = (async (): Promise<SessionResourceRefreshResult> => {
        setState((prev) => {
          if (ownerRef.current !== owner || prev.currentSession?.session_id !== sessionId) return prev;
          return {
            ...prev,
            sessionResourcesLoading: silent ? prev.sessionResourcesLoading : true,
            sessionResourcesError: null,
            status: failureStatus ?? (silent ? prev.status : "正在读取后台连接"),
          };
        });

        try {
          const resources = await getSessionResources(apiPort, sessionId, workspaceId);
          if (ownerRef.current !== owner || requestId !== requestIdRef.current) return "stale";
          setState((prev) => {
            if (
              ownerRef.current !== owner ||
              requestId !== requestIdRef.current ||
              prev.currentSession?.session_id !== sessionId
            ) {
              return prev;
            }
            const previousCount = prev.sessionResources.length;
            const nextCount = resources.items.length;
            const status =
              silent && previousCount === nextCount
                ? prev.status
                : silent
                  ? `后台连接已更新 (${nextCount} 个)`
                  : `后台连接已加载 (${nextCount} 个)`;
            return {
              ...prev,
              sessionResources: resources.items,
              sessionResourcesLoadedAt: new Date().toISOString(),
              sessionResourcesLoading: false,
              sessionResourcesError: null,
              status: failureStatus ?? status,
            };
          });
          return "loaded";
        } catch (error) {
          const message = errorMessage(error);
          if (ownerRef.current !== owner || requestId !== requestIdRef.current) return "stale";
          setState((prev) => {
            if (
              ownerRef.current !== owner ||
              requestId !== requestIdRef.current ||
              prev.currentSession?.session_id !== sessionId
            ) {
              return prev;
            }
            return {
              ...prev,
              sessionResourcesLoading: false,
              sessionResourcesError: message,
              status: failureStatus ?? (silent ? prev.status : `后台连接加载失败: ${message}`),
            };
          });
          return "failed";
        }
      })();
      inFlightRefreshRef.current = { key: requestKey, promise: request };
      try {
        return await request;
      } finally {
        if (inFlightRefreshRef.current?.promise === request) {
          inFlightRefreshRef.current = null;
        }
      }
    },
    [apiPort, workspaceId, setState],
  );

  const refreshSessionResources = useCallback(
    (sessionId: string, options: RefreshOptions = {}) => readSessionResources(sessionId, options, null),
    [readSessionResources],
  );

  const controlSessionResource = useCallback(
    async (
      kind: SessionResourceKind,
      resourceId: string,
      action: SessionResourceAction,
    ) => {
      if (!currentSession) {
        throw new Error("当前没有可控制资源的会话");
      }

      const sessionId = currentSession.session_id;
      const owner = ownerRef.current;
      if (owner.sessionId !== sessionId || owner.apiPort !== apiPort || owner.workspaceId !== workspaceId) {
        throw new Error("当前会话资源请求已失效");
      }
      const actionLabel = actionLabelForKind(kind, action);
      setState((prev) => ({
        ...prev,
        status: `正在${actionLabel}`,
      }));

      let result: Awaited<ReturnType<typeof apiControlSessionResource>>;
      try {
        result = await apiControlSessionResource(
          apiPort,
          sessionId,
          kind,
          resourceId,
          action,
          workspaceId,
        );
      } catch (error) {
        const message = errorMessage(error);
        const failureStatus = `${actionLabel}失败: ${message}`;
        if (ownerRef.current === owner) {
          // 权威重读完成前保持原控制错误；重读失败另存完整读取诊断。
          await readSessionResources(sessionId, {}, failureStatus);
        }
        throw error;
      }

      setState((prev) => {
        if (ownerRef.current !== owner || prev.currentSession?.session_id !== sessionId) {
          return prev;
        }
        const nextResources = prev.sessionResources.flatMap((resource) => {
          const isTarget =
            resource.kind === kind && resource.resource_id === resourceId;
          if (!isTarget) {
            return [resource];
          }
          if (result.resource) {
            return [result.resource];
          }
          return action === "delete" ? [] : [resource];
        });
        return {
          ...prev,
          sessionResources: nextResources,
          sessionResourcesLoadedAt: new Date().toISOString(),
          status: `${resourceActionStatusLabel(kind, action)}完成：${statusLabel(result.status)}，当前 ${nextResources.length} 个资源`,
        };
      });
    },
    [apiPort, currentSession, workspaceId, setState, readSessionResources],
  );

  return {
    controlSessionResource,
    invalidateSessionResources,
    refreshSessionResources,
  };
}
