import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { errorMessage } from "../../utils/errorMessage";

export function usePersistentTasks(port: number, target: GatewayWorkspaceRequestTarget, sessionId?: string) {
  const owner = useMemo(() => ({ ...target }), [target.workspace_id, target.backend_workspace_id, target.connection_kind]);
  const scope = useMemo(() => ({ controller: new AbortController(), reader: new AbortController(), busy: false }), [port, owner, sessionId]);
  const current = useRef(scope);
  current.current = scope;
  const [version, setVersion] = useState(0);
  const [view, setView] = useState({ scope, items: [] as api.PersistentTaskDTO[], loaded: false, error: null as string | null, busy: false });
  const [selection, setSelection] = useState({ scope, id: "" });
  const items = view.scope === scope ? view.items : [];
  const selected = items.find((item) => selection.scope === scope && item.task_id === selection.id) ?? null;
  const [history, setHistory] = useState({ scope, taskId: "", runs: [] as api.PersistentTaskRunDTO[], loading: false, error: null as string | null });

  useEffect(() => {
    const controller = new AbortController();
    scope.controller = controller;
    setView({ scope, items: [], loaded: false, error: null, busy: false });
    setSelection({ scope, id: "" });
    return () => { controller.abort(); scope.reader.abort(); };
  }, [scope]);

  useEffect(() => {
    const reader = new AbortController();
    scope.reader = reader;
    const valid = () => current.current === scope && !reader.signal.aborted;
    const publish = (next: api.PersistentTaskDTO[]) => {
      if (valid()) setView((previous) => ({ ...previous, scope, items: next, loaded: true }));
    };
    void (async () => {
      try {
        if (valid()) await api.watchPersistentTasks(port, owner, reader.signal, publish, sessionId);
      } catch (cause) {
        if (valid()) setView((previous) => ({ ...previous, scope, error: errorMessage(cause) }));
      }
    })();
    return () => reader.abort();
  }, [port, owner, scope, version, sessionId]);

  useEffect(() => {
    const controller = new AbortController();
    const taskId = selected?.task_id ?? "";
    setHistory({ scope, taskId, runs: [], loading: Boolean(selected && selected.application_type === "automation"), error: null });
    if (selected?.application_type === "automation") {
      void api.listPersistentTaskRuns(port, owner, taskId, controller.signal).then((runs) => {
        if (!controller.signal.aborted && current.current === scope) setHistory({ scope, taskId, runs, loading: false, error: null });
      }, (cause: unknown) => {
        if (!controller.signal.aborted && current.current === scope) setHistory({ scope, taskId, runs: [], loading: false, error: errorMessage(cause) });
      });
    }
    return () => controller.abort();
  }, [port, owner, scope, selected, version]);

  const act = useCallback(async (operation: () => Promise<api.PersistentTaskDTO | null>) => {
    if (current.current !== scope || scope.controller.signal.aborted || scope.busy) return false;
    scope.busy = true;
    scope.reader.abort();
    setView((previous) => ({ ...previous, scope, busy: true, error: null }));
    let succeeded = false;
    try {
      const result = await operation();
      if (current.current === scope && !scope.controller.signal.aborted) {
        if (result) {
          setView((previous) => ({ ...previous, items: [result, ...previous.items.filter((item) => item.task_id !== result.task_id)] }));
          setSelection({ scope, id: result.task_id });
        }
        succeeded = true;
      }
    } catch (cause) {
      if (current.current === scope && !scope.controller.signal.aborted) setView((previous) => ({ ...previous, error: errorMessage(cause) }));
    } finally {
      // 失败也读回完整列表；状态流在写入后重新建立，旧快照不能覆盖写入回执。
      if (current.current === scope && !scope.controller.signal.aborted) {
        try {
          const next = await api.listPersistentTasks(port, owner, scope.controller.signal, sessionId);
          if (current.current === scope && !scope.controller.signal.aborted) setView((previous) => ({ ...previous, items: next, loaded: true }));
        } catch (cause) {
          if (current.current === scope && !scope.controller.signal.aborted) setView((previous) => ({ ...previous, error: [previous.error, `刷新失败：${errorMessage(cause)}`].filter(Boolean).join("；") }));
        }
        if (current.current === scope && !scope.controller.signal.aborted) {
          scope.busy = false;
          setView((previous) => ({ ...previous, busy: false }));
          setVersion((value) => value + 1);
        }
      }
    }
    return succeeded;
  }, [port, owner, scope, sessionId]);

  const select = useCallback((id: string) => setSelection({ scope, id }), [scope]);

  return {
    items, selected, loaded: view.scope === scope && view.loaded, busy: view.scope === scope && view.busy,
    error: view.scope === scope ? view.error : null,
    history: history.scope === scope && history.taskId === selected?.task_id ? history : null,
    select,
    refresh: () => act(async () => null),
    create: (configuration: api.PersistentTaskConfigurationDTO, key: string) => act(() => api.createPersistentTask(port, owner, configuration, key, scope.controller.signal)),
    update: (task: api.PersistentTaskDTO, configuration: api.PersistentTaskConfigurationDTO) => act(() => api.updatePersistentTask(port, owner, task, configuration, scope.controller.signal)),
    control: (task: api.PersistentTaskDTO, action: api.TaskControl) => act(() => api.controlPersistentTask(port, owner, task, action, scope.controller.signal)),
    check: (task: api.PersistentTaskDTO, checkOnly: boolean) => act(async () => { await api.checkPersistentTask(port, owner, task, checkOnly, scope.controller.signal); return null; }),
  };
}
