import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { errorMessage } from "../../utils/errorMessage";
import { requestTaskMutation, settleTaskOperation, type TaskMutation } from "./taskActions";

export function usePersistentTasks(port: number, target: GatewayWorkspaceRequestTarget, sessionId?: string, active = true) {
  const owner = useMemo(() => ({ ...target }), [target.gateway_id, target.workspace_id]);
  const scope = useMemo(() => ({ controller: new AbortController(), reader: new AbortController(), busy: false }), [port, owner, sessionId]);
  const current = useRef(scope);
  current.current = scope;
  const [version, setVersion] = useState(0);
  const [view, setView] = useState({ scope, items: [] as api.PersistentTaskDTO[], runtime: null as api.TaskRuntimeStatusDTO | null, loaded: false, error: null as string | null, busy: false });
  const [selection, setSelection] = useState({ scope, id: "" });
  const items = view.scope === scope ? view.items : [];
  const selected = items.find((item) => selection.scope === scope && item.task_id === selection.id) ?? null;
  const [history, setHistory] = useState({ scope, taskId: "", runs: [] as api.PersistentTaskRunDTO[], loading: false, error: null as string | null });

  useEffect(() => {
    const controller = new AbortController();
    scope.controller = controller;
    setView({ scope, items: [], runtime: null, loaded: false, error: null, busy: false });
    setSelection({ scope, id: "" });
    return () => { controller.abort(); scope.reader.abort(); };
  }, [scope]);

  useEffect(() => {
    if (!active) return;
    const reader = new AbortController();
    scope.reader = reader;
    const valid = () => current.current === scope && !reader.signal.aborted;
    const publish = (next: api.PersistentTaskSnapshot) => {
      if (valid()) setView((previous) => ({ ...previous, scope, ...next, loaded: true }));
    };
    void (async () => {
      try {
        if (valid()) await api.watchPersistentTasks(port, owner, reader.signal, publish, sessionId);
      } catch (cause) {
        if (valid()) setView((previous) => ({ ...previous, scope, error: errorMessage(cause) }));
      }
    })();
    return () => reader.abort();
  }, [port, owner, scope, version, sessionId, active]);

  useEffect(() => {
    const controller = new AbortController();
    const taskId = selected?.task_id ?? "";
    setHistory({ scope, taskId, runs: [], loading: Boolean(selected), error: null });
    if (selected) {
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
    const valid = () => current.current === scope && !scope.controller.signal.aborted;
    const result = await settleTaskOperation(port, owner, scope.controller.signal, operation, (result) => {
      if (valid()) {
        setView((previous) => ({ ...previous, items: [result, ...previous.items.filter((item) => item.task_id !== result.task_id)] }));
        setSelection({ scope, id: result.task_id });
      }
    }, sessionId);
    if (valid()) {
      scope.busy = false;
      setView((previous) => ({ ...previous, ...(result.snapshot ?? {}), loaded: Boolean(result.snapshot) || previous.loaded, error: result.error, busy: false }));
      setVersion((value) => value + 1);
    }
    return valid() && result.succeeded;
  }, [port, owner, scope, sessionId]);

  const select = useCallback((id: string) => setSelection({ scope, id }), [scope]);

  return {
    items, selected, loaded: view.scope === scope && view.loaded, busy: view.scope === scope && view.busy,
    error: view.scope === scope ? view.error : null,
    runtime: view.scope === scope ? view.runtime : null,
    history: history.scope === scope && history.taskId === selected?.task_id ? history : null,
    select,
    isCurrent: () => current.current === scope && !scope.controller.signal.aborted,
    refresh: () => act(async () => null),
    create: (configuration: api.PersistentTaskConfigurationDTO, key: string) => act(() => api.createPersistentTask(port, owner, configuration, key, scope.controller.signal)),
    run: (task: api.PersistentTaskDTO, action: TaskMutation, configuration?: api.PersistentTaskConfigurationDTO) =>
      act(() => requestTaskMutation(port, owner, task, action, scope.controller.signal, configuration)),
  };
}
