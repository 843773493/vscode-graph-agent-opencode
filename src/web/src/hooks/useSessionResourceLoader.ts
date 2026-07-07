import { useCallback, useRef } from "react";
import {
  controlSessionResource as apiControlSessionResource,
  getSessionResources,
} from "../api";
import type {
  Session,
  SessionResourceAction,
  SessionResourceKind,
} from "../types/backend";
import {
  actionLabelForKind,
  resourceActionStatusLabel,
  statusLabel,
} from "../state/resourceDisplay";
import type { RefreshOptions, SetAppState } from "./contentViewLoaderTypes";

export function useSessionResourceLoader({
  apiPort,
  currentSession,
  setState,
}: {
  apiPort: number;
  currentSession: Session | null;
  setState: SetAppState;
}) {
  const requestIdRef = useRef(0);

  const invalidateSessionResources = useCallback(() => {
    requestIdRef.current += 1;
  }, []);

  const refreshSessionResources = useCallback(
    async (sessionId: string, options: RefreshOptions = {}) => {
      const silent = options.silent === true;
      const requestId = requestIdRef.current + 1;
      requestIdRef.current = requestId;
      setState((prev) => ({
        ...prev,
        contentView: "resources",
        sessionResourcesLoading: silent ? prev.sessionResourcesLoading : true,
        sessionResourcesError: null,
        status: silent ? prev.status : "正在读取会话资源",
      }));

      try {
        const resources = await getSessionResources(apiPort, sessionId);
        setState((prev) => {
          if (
            requestId !== requestIdRef.current ||
            prev.currentSession?.session_id !== sessionId ||
            prev.contentView !== "resources"
          ) {
            return prev;
          }
          const previousCount = prev.sessionResources.length;
          const nextCount = resources.items.length;
          const status =
            silent && previousCount === nextCount
              ? prev.status
              : silent
                ? `会话资源已更新 (${nextCount} 个)`
                : `会话资源已加载 (${nextCount} 个)`;
          return {
            ...prev,
            contentView: "resources",
            sessionResources: resources.items,
            sessionResourcesLoadedAt: new Date().toISOString(),
            sessionResourcesLoading: false,
            sessionResourcesError: null,
            status,
          };
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => {
          if (
            requestId !== requestIdRef.current ||
            prev.currentSession?.session_id !== sessionId ||
            prev.contentView !== "resources"
          ) {
            return prev;
          }
          return {
            ...prev,
            contentView: "resources",
            sessionResourcesLoading: false,
            sessionResourcesError: message,
            status: silent ? prev.status : `会话资源加载失败: ${message}`,
          };
        });
      }
    },
    [apiPort, setState],
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
      setState((prev) => ({
        ...prev,
        status: `正在${actionLabelForKind(kind, action)}`,
      }));

      const result = await apiControlSessionResource(
        apiPort,
        sessionId,
        kind,
        resourceId,
        action,
      );

      setState((prev) => {
        if (prev.currentSession?.session_id !== sessionId) {
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
    [apiPort, currentSession, setState],
  );

  return {
    controlSessionResource,
    invalidateSessionResources,
    refreshSessionResources,
  };
}
