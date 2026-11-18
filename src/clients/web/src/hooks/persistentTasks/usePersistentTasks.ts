import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { errorMessage } from "../../utils/errorMessage";
import { requestTaskMutation, settleTaskOperation, type TaskMutation } from "./taskActions";

export function usePersistentTasks(port: number, target: GatewayWorkspaceRequestTarget, sessionId?: string, active = true, observe?: (event: api.TaskObservation) => void) {
  const owner = useMemo(() => ({ ...target }), [target.gateway_id, target.workspace_id]);
  const scope = useMemo(() => ({ controller: new AbortController(), reader: new AbortController(), busy: false, observation: 0 }), [port, owner, sessionId]);
  const current = useRef(scope);
  current.current = scope;
  const [version, setVersion] = useState(0);
  const observer = useRef(observe); observer.current = observe;
  const [historyVersion, setHistoryVersion] = useState(0);
  const [view, setView] = useState({ scope, items: [] as api.PersistentTaskDTO[], runtime: null as api.TaskRuntimeStatusDTO | null,
    session_goal: null as api.PersistentTaskSnapshot["session_goal"], loaded: false,
    error: null as string | null, streamError: null as string | null, busy: false });
  const [selection, setSelection] = useState({ scope, id: "" });
  const items = view.scope === scope ? view.items : [];
  const selected = items.find((item) => selection.scope === scope && item.task_id === selection.id) ?? null;
  const [history, setHistory] = useState({ scope, taskId: "", runs: [] as api.PersistentTaskRunSummaryDTO[], nextBefore: undefined as string | undefined, loading: false, error: null as string | null });
  const historyRequest = useRef<AbortController>();
  const latestHistory = useRef(history); latestHistory.current = history;
  const taskId = selected?.task_id ?? "";

  useEffect(() => {
    const controller = new AbortController();
    scope.controller = controller;
    setView({ scope, items: [], runtime: null, session_goal: null, loaded: false, error: null, streamError: null, busy: false });
    setSelection({ scope, id: "" });
    return () => { controller.abort(); scope.reader.abort(); };
  }, [scope]);

  useEffect(() => {
    if (!active) return;
    const reader = new AbortController();
    scope.reader = reader;
    const valid = () => current.current === scope && !reader.signal.aborted;
    const publish = (next: api.PersistentTaskSnapshot) => {
      if (!valid()) return;
      scope.observation += 1;
      setView((previous) => ({ ...previous, scope, ...next, loaded: true, streamError: null }));
      if (next.history_invalidated !== false) setHistoryVersion((value) => value + 1);
      observer.current?.({ snapshot: next });
    };
    const changed = (next: api.PersistentTaskChangedDTO) => {
      if (!valid()) return;
      scope.observation += 1;
      setView((previous) => ({ ...previous, items: next.task
        ? previous.items.some((item) => item.task_id === next.task_id)
          ? previous.items.map((item) => item.task_id === next.task_id ? next.task! : item)
          : [...previous.items, next.task]
        : previous.items.filter((item) => item.task_id !== next.task_id) }));
      const run = next.run;
      if (run) setHistory((previous) => {
        if (previous.scope !== scope || previous.taskId !== run.task_id) return previous;
        const existing = previous.runs.some((item) => item.run_id === run.run_id);
        if (!existing && previous.runs.length && run.run_sequence < previous.runs[0].run_sequence) return previous;
        return { ...previous, runs: [...previous.runs.filter((item) => item.run_id !== run.run_id), run].sort((a, b) => a.run_sequence - b.run_sequence) };
      });
      observer.current?.({ changed: next });
    };
    void (async () => {
      try {
        if (valid()) await api.watchPersistentTasks(port, owner, reader.signal, publish, changed, sessionId);
      } catch (cause) {
        if (valid()) {
          reader.abort();
          setView((previous) => ({ ...previous, scope, streamError: errorMessage(cause) }));
        }
      }
    })();
    return () => reader.abort();
  }, [port, owner, scope, version, sessionId, active]);

  const readHistory = useCallback((before?: string) => {
    if (!active || !taskId || current.current !== scope) return;
    historyRequest.current?.abort();
    const controller = new AbortController();
    historyRequest.current = controller;
    const updates = new Map<string, api.PersistentTaskRunSummaryDTO>();
    const baseline = latestHistory.current;
    setHistory((previous) => ({ ...previous, scope, taskId,
      runs: previous.scope === scope && previous.taskId === taskId ? previous.runs : [],
      nextBefore: previous.scope === scope && previous.taskId === taskId ? previous.nextBefore : undefined,
      loading: true, error: null }));
    void api.listPersistentTaskRuns(port, owner, taskId, controller.signal, before).then((page) => {
        if (!controller.signal.aborted && current.current === scope) setHistory((previous) => {
          // 请求期间到达的实时更新优先，避免较早开始的分页响应覆盖新状态。
          for (const run of previous.runs) if (!baseline.runs.includes(run)) updates.set(run.run_id, run);
          const runs = new Map((before ? previous.runs : []).map((run) => [run.run_id, run]));
          for (const run of page.items) runs.set(run.run_id, updates.get(run.run_id) ?? run);
          for (const run of updates.values()) if (!page.items.length || run.run_sequence >= page.items[0].run_sequence) runs.set(run.run_id, run);
          return { scope, taskId, runs: [...runs.values()].sort((a, b) => a.run_sequence - b.run_sequence), nextBefore: page.next_before, loading: false, error: null };
        });
      }, (cause: unknown) => {
        if (!controller.signal.aborted && current.current === scope) setHistory((previous) => ({ ...previous, loading: false, error: errorMessage(cause) }));
      });
  }, [port, owner, scope, taskId, active]);

  useEffect(() => {
    readHistory();
    return () => historyRequest.current?.abort();
  }, [readHistory, historyVersion]);

  const act = useCallback(async (operation: () => Promise<api.PersistentTaskDTO | null>, reconnect = false) => {
    if (current.current !== scope || scope.controller.signal.aborted || scope.busy) return false;
    scope.busy = true;
    // 操作等待期间继续接收运行更新；较早的回执和回读不能覆盖这些新观察。
    const observation = scope.observation;
    setView((previous) => ({ ...previous, scope, busy: true, error: null }));
    const valid = () => current.current === scope && !scope.controller.signal.aborted;
    const result = await settleTaskOperation(port, owner, scope.controller.signal, operation, (result) => {
      if (valid()) {
        if (scope.observation === observation) setView((previous) => ({ ...previous, items: [result, ...previous.items.filter((item) => item.task_id !== result.task_id)] }));
        setSelection({ scope, id: result.task_id });
      }
    }, sessionId);
    if (valid()) {
      scope.busy = false;
      setView((previous) => ({ ...previous, ...(scope.observation === observation ? result.snapshot ?? {} : {}), loaded: Boolean(result.snapshot) || previous.loaded, error: result.error, busy: false }));
      if (reconnect || scope.reader.signal.aborted) setVersion((value) => value + 1);
    }
    return valid() && result.succeeded;
  }, [port, owner, scope, sessionId]);

  const select = useCallback((id: string) => setSelection({ scope, id }), [scope]);

  return {
    items, selected, loaded: view.scope === scope && view.loaded, busy: view.scope === scope && view.busy,
    error: view.scope === scope ? view.error ?? view.streamError : null,
    runtime: view.scope === scope ? view.runtime : null,
    sessionGoal: view.scope === scope ? view.session_goal : null,
    history: history.scope === scope && history.taskId === selected?.task_id ? history : null,
    select,
    loadOlder: () => { if (history.nextBefore && !history.loading) readHistory(history.nextBefore); },
    isCurrent: () => current.current === scope && !scope.controller.signal.aborted,
    refresh: () => act(async () => null, true),
    create: (configuration: api.PersistentTaskConfigurationDTO, key: string) => act(() => api.createPersistentTask(port, owner, configuration, key, scope.controller.signal)),
    run: (task: api.PersistentTaskDTO, action: TaskMutation, configuration?: api.PersistentTaskConfigurationDTO) =>
      act(() => requestTaskMutation(port, owner, task, action, scope.controller.signal, configuration)),
  };
}
