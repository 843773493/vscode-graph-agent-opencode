import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { controlWorkspaceResource, createWorkspaceBrowser, createWorkspaceTerminal, listWorkspaceResources } from "../../api/workspaceResources";
import type { ResourceAction, ResourceThreadRef, WorkspaceConnectionKind, WorkspaceResourceDTO } from "../../api/workspaceResources";
import { errorMessage } from "../../utils/errorMessage";

export function useWorkspaceResources(apiPort: number, workspaceId: string | null, kind: WorkspaceConnectionKind, enabled: boolean) {
  const [items, setItems] = useState<WorkspaceResourceDTO[]>([]);
  const [loading, setLoading] = useState(false);
  const [mutating, setMutating] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const scope = useMemo(() => ({ apiPort, workspaceId, kind, generation: 0, mutating: false }), [apiPort, workspaceId, kind]);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const replace = useCallback((resource: WorkspaceResourceDTO) => {
    setItems((current) => resource.status === "deleted"
      ? current.filter((item) => item.resource_id !== resource.resource_id)
      : [resource, ...current.filter((item) => item.resource_id !== resource.resource_id)]);
  }, []);
  const refresh = useCallback(async (explicit = true) => {
    if (!workspaceId || scope.mutating) return;
    const request = ++scope.generation;
    setLoading(true);
    try {
      const result = await listWorkspaceResources(apiPort, workspaceId, kind);
      if (scopeRef.current === scope && request === scope.generation) {
        setItems(result.items);
        setReadError(null);
        if (explicit) setOperationError(null);
      }
    } catch (cause) {
      if (scopeRef.current === scope && request === scope.generation) setReadError(errorMessage(cause));
    } finally {
      if (scopeRef.current === scope && request === scope.generation) setLoading(false);
    }
  }, [apiPort, scope, workspaceId, kind]);
  useEffect(() => {
    setItems([]);
    setReadError(null);
    setOperationError(null);
    setLoading(false);
    setMutating(false);
    return () => { ++scope.generation; };
  }, [scope]);
  useEffect(() => {
    if (!enabled || !workspaceId) return;
    void refresh(false);
    const timer = window.setInterval(() => void refresh(false), 2000);
    return () => { ++scope.generation; window.clearInterval(timer); };
  }, [enabled, refresh, scope, workspaceId]);
  const mutate = useCallback(async (operation: () => Promise<WorkspaceResourceDTO>) => {
    if (!workspaceId || scopeRef.current !== scope) throw new Error("资源管理缺少当前工作区");
    if (scope.mutating) throw new Error("当前资源操作尚未完成，请稍后重试");
    // 管理写入开始即作废在途刷新；轮询不能用旧清单覆盖完整 owner 回执。
    ++scope.generation;
    scope.mutating = true;
    setMutating(true);
    setLoading(false);
    try {
      const result = await operation();
      if (scopeRef.current !== scope) throw new Error("资源操作已在原工作区完成；当前资源范围已切换");
      replace(result);
      setOperationError(null);
      return result;
    } catch (cause) {
      if (scopeRef.current === scope) {
        let message = errorMessage(cause);
        try {
          const result = await listWorkspaceResources(apiPort, workspaceId, kind);
          if (scopeRef.current === scope) { setItems(result.items); setReadError(null); }
        } catch (refreshCause) {
          message += `；刷新失败：${errorMessage(refreshCause)}`;
        }
        if (scopeRef.current === scope) setOperationError(message);
      }
      throw cause;
    } finally {
      scope.mutating = false;
      if (scopeRef.current === scope) setMutating(false);
    }
  }, [apiPort, replace, scope, workspaceId, kind]);
  const control = useCallback((resource: WorkspaceResourceDTO, action: ResourceAction, thread?: ResourceThreadRef) =>
    mutate(() => controlWorkspaceResource(apiPort, workspaceId!, resource, action, thread)), [apiPort, mutate, workspaceId]);
  const create = useCallback((thread: ResourceThreadRef, retentionScope: "thread" | "workspace") =>
    mutate(() => (kind === "terminal" ? createWorkspaceTerminal : createWorkspaceBrowser)(apiPort, workspaceId!, thread, retentionScope)), [apiPort, mutate, workspaceId, kind]);
  return { items, loading, mutating, error: [operationError, readError].filter(Boolean).join("；") || null, refresh, control, create };
}

export type WorkspaceResourcesController = ReturnType<typeof useWorkspaceResources>;
