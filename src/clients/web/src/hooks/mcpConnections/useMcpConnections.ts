import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { cancelMcpOperation, listMcpOperations, listMcpServers, submitMcpOperation, type McpConnectionOperationDTO, type McpServerDTO } from "../../api/mcpConnections";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { errorMessage } from "../../utils/errorMessage";
import { authorizeMcpInWindow } from "./authorizationWindow";

export function useMcpConnections(port: number, target: GatewayWorkspaceRequestTarget) {
  const requestTarget = useMemo(() => ({ ...target }), [target.workspace_id, target.backend_workspace_id, target.connection_kind]);
  const scope = useMemo(() => ({ controller: new AbortController(), generation: 0, busy: false, reading: 0 }), [port, requestTarget]);
  const [view, setView] = useState({ scope, servers: [] as McpServerDTO[], operations: [] as McpConnectionOperationDTO[], error: null as string | null, busy: false, loaded: false });
  const current = useRef(scope);
  current.current = scope;
  const refresh = useCallback(async () => {
    const generation = ++scope.generation;
    const controller = scope.controller;
    ++scope.reading;
    try {
      const [nextServers, nextOperations] = await Promise.all([
        listMcpServers(port, requestTarget, controller.signal), listMcpOperations(port, requestTarget, controller.signal),
      ]);
      if (current.current === scope && generation === scope.generation && !controller.signal.aborted) {
        setView((previous) => ({ ...previous, scope, servers: nextServers, operations: nextOperations, loaded: true }));
      }
    } finally { --scope.reading; }
  }, [port, scope, requestTarget]);
  useEffect(() => {
    const controller = new AbortController();
    scope.controller = controller;
    scope.busy = false;
    setView({ scope, servers: [], operations: [], error: null, busy: false, loaded: false });
    const poll = async () => {
      if (scope.busy || scope.reading) return;
      try { await refresh(); }
      catch (cause) { if (current.current === scope && !controller.signal.aborted) setView((previous) => ({ ...previous, error: errorMessage(cause) })); }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2000);
    return () => { ++scope.generation; controller.abort(); window.clearInterval(timer); };
  }, [refresh, scope]);
  const act = useCallback(async (action: () => Promise<McpConnectionOperationDTO | void>) => {
    if (scope.busy || current.current !== scope || scope.controller.signal.aborted) return;
    ++scope.generation;
    const controller = scope.controller;
    scope.busy = true; setView((previous) => ({ ...previous, busy: true, error: null }));
    try {
      const operation = await action();
      if (operation && current.current === scope && !controller.signal.aborted) {
        setView((previous) => ({ ...previous, operations: [operation, ...previous.operations.filter((item) => item.server_id !== operation.server_id)] }));
      }
    } catch (cause) {
      if (current.current === scope && !controller.signal.aborted) setView((previous) => ({ ...previous, error: errorMessage(cause) }));
    } finally {
      // 无论写入是否成功，重新读取实际 owner；旧读取不能覆盖完整写入回执。
      if (current.current === scope && !controller.signal.aborted) {
        try { await refresh(); }
        catch (cause) { if (!controller.signal.aborted) setView((previous) => ({ ...previous, error: [previous.error, `刷新失败：${errorMessage(cause)}`].filter(Boolean).join("；") })); }
        scope.busy = false;
        if (!controller.signal.aborted) setView((previous) => ({ ...previous, busy: false }));
      }
    }
  }, [refresh, scope]);
  return {
    servers: view.scope === scope ? view.servers : [], operations: view.scope === scope ? view.operations : [],
    error: view.scope === scope ? view.error : null, busy: view.scope === scope && view.busy, loaded: view.scope === scope && view.loaded,
    refresh: () => act(async () => {}),
    submit: (serverId: string, action: "connect" | "disconnect") => act(() => submitMcpOperation(port, requestTarget, serverId, action, scope.controller.signal)),
    cancel: (operation: McpConnectionOperationDTO) => act(() => cancelMcpOperation(port, requestTarget, operation.operation_id, scope.controller.signal)),
    authorize: (operation: McpConnectionOperationDTO) => act(() => authorizeMcpInWindow(port, requestTarget, operation, scope.controller.signal)),
  };
}
