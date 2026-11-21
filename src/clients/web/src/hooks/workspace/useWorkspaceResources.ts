import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { t } from "../../../../shared/core/i18n/index.js";
import { controlWorkspaceResource, createWorkspaceBrowser, createWorkspaceTerminal, listWorkspaceResources, observeTerminalResources } from "../../api/workspaceResources";
import type { ResourceAction, ResourceThreadRef, WorkspaceConnectionKind, WorkspaceResourceDTO } from "../../api/workspaceResources";
import { errorMessage } from "../../utils/errorMessage";
import { HttpRequestError } from "../../api/http";
import { settleWorkspaceResourceOperation, type DispatchWorkspaceResourceAction } from "./resourceActions";

export function useWorkspaceResources(apiPort: number, workspaceId: string | null, kind: WorkspaceConnectionKind, enabled: boolean,
  dispatchControl: DispatchWorkspaceResourceAction) {
  const [items, setItems] = useState<WorkspaceResourceDTO[]>([]);
  const [loading, setLoading] = useState(false);
  const [mutating, setMutating] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const scope = useMemo(() => ({ apiPort, workspaceId, kind, generation: 0, mutating: false, invalidated: false, observing: false,
    retry: undefined as ReturnType<typeof setTimeout> | undefined,
    pending: null as ReturnType<typeof listWorkspaceResources> | null }), [apiPort, workspaceId, kind]);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const replace = useCallback((resource: WorkspaceResourceDTO) => {
    setItems((current) => resource.status === "deleted"
      ? current.filter((item) => item.resource_id !== resource.resource_id)
      : [resource, ...current.filter((item) => item.resource_id !== resource.resource_id)]);
  }, []);
  const refresh = useCallback(async (explicit = true, attempt = 0): Promise<void> => {
    if (!workspaceId) return;
    clearTimeout(scope.retry);
    if (scope.mutating || scope.pending) { scope.invalidated = true; return; }
    scope.invalidated = false;
    const request = ++scope.generation;
    setLoading(true);
    const pending = scope.pending ??= listWorkspaceResources(apiPort, workspaceId, kind);
    try {
      const result = await pending;
      if (scopeRef.current === scope && request === scope.generation) {
        setItems(result.items);
        setReadError(null);
        if (explicit) setOperationError(null);
      }
    } catch (cause) {
      if (scopeRef.current === scope && request === scope.generation) {
        setReadError(errorMessage(cause));
        // 只恢复失败的读取，最多两次；写入不重放，也不恢复周期轮询。
        if (scope.observing && attempt < 2 && (!(cause instanceof HttpRequestError)
          || cause.status === 409 || cause.status >= 500)) {
          scope.retry = setTimeout(() => void refresh(false, attempt + 1), 1000 * 2 ** attempt);
        }
      }
    } finally {
      if (scope.pending === pending) {
        scope.pending = null;
        if (scopeRef.current === scope && request === scope.generation) {
          setLoading(false);
          if (scope.invalidated && !scope.mutating) void refresh(false);
        }
      }
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
    if (kind === "browser") {
      void refresh(false);
      const timer = window.setInterval(() => void refresh(false), 2000);
      return () => { ++scope.generation; scope.pending = null; scope.invalidated = false; scope.observing = false; clearTimeout(scope.retry); window.clearInterval(timer); };
    }
    scope.observing = true;
    const controller = new AbortController();
    let retry: ReturnType<typeof setTimeout> | undefined;
    const observe = async () => {
      try {
        await observeTerminalResources(apiPort, workspaceId, () => void refresh(false), controller.signal);
        if (!controller.signal.aborted) throw new Error("终端资源观察流已结束");
      } catch (cause) {
        if (controller.signal.aborted) return;
        setReadError(errorMessage(cause));
        retry = setTimeout(() => void observe(), 5000);
      }
    };
    void observe();
    return () => { ++scope.generation; scope.pending = null; scope.invalidated = false; scope.observing = false; clearTimeout(scope.retry); controller.abort(); clearTimeout(retry); };
  }, [enabled, refresh, scope, workspaceId, kind, apiPort]);
  const mutate = useCallback(async (operation: () => Promise<WorkspaceResourceDTO>) => {
    if (!workspaceId || scopeRef.current !== scope) throw new Error(t("resources.errors.missingWorkspace"));
    if (scope.mutating) throw new Error(t("resources.errors.operationInProgress"));
    // 管理写入开始即作废在途刷新；旧清单不能覆盖完整 owner 回执。
    ++scope.generation;
    scope.pending = null;
    scope.mutating = true;
    setMutating(true);
    setLoading(false);
    try {
      const settled = await settleWorkspaceResourceOperation(apiPort, workspaceId, kind, operation, () => scopeRef.current === scope);
      if (!settled.succeeded) {
        if (scopeRef.current === scope) {
          if (settled.snapshot) { setItems(settled.snapshot.items); setReadError(null); }
          setOperationError(settled.error);
        }
        throw settled.cause;
      }
      const result = settled.resource;
      if (scopeRef.current !== scope) throw new Error(t("resources.errors.workspaceChanged"));
      replace(result);
      setOperationError(null);
      return result;
    } finally {
      scope.mutating = false;
      if (scopeRef.current === scope) setMutating(false);
      if (scopeRef.current === scope && scope.invalidated) void refresh(false);
    }
  }, [apiPort, replace, refresh, scope, workspaceId, kind]);
  const control = useCallback(async (resource: WorkspaceResourceDTO, action: ResourceAction, thread?: ResourceThreadRef) => {
    const run = (selected: ResourceAction, ref?: ResourceThreadRef) => mutate(() => controlWorkspaceResource(apiPort, workspaceId!, resource, selected, ref));
    const result = await dispatchControl(action, { workspaceKey: workspaceId!, resource, run }, thread);
    // 原组件只在写入确已执行时消费完整回执；命令取消不伪造资源。
    if (!result) throw new Error(t("resources.errors.operationCancelled"));
    return result;
  }, [apiPort, dispatchControl, mutate, workspaceId]);
  const create = useCallback((thread: ResourceThreadRef, retentionScope: "thread" | "workspace") =>
    mutate(() => (kind === "terminal" ? createWorkspaceTerminal : createWorkspaceBrowser)(apiPort, workspaceId!, thread, retentionScope)), [apiPort, mutate, workspaceId, kind]);
  return { items, loading, mutating, error: [operationError, readError].filter(Boolean).join("；") || null, refresh, control, create };
}

export type WorkspaceResourcesController = ReturnType<typeof useWorkspaceResources>;
