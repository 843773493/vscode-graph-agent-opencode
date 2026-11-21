import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createSerialOperationQueue, createSnapshotReadCoordinator, SnapshotReadSupersededError } from "../../../../shared/core/syncState";
import { clearSessionGoal, getSessionGoalOperation, updateSessionGoal } from "../../api/session/sessionGoals";
import { listPersistentTasks, watchPersistentTasks, type PersistentTaskSnapshot, type PersistentTaskDTO } from "../../api/persistentTasks";
import { workspaceOwnerFromKey } from "../../api/workspaceIdentity";
import { assertGoalOwner, parseSessionGoal, parseSessionGoalSnapshot } from "../../protocol/sessionGoal";
import type { SessionGoalOperationResultDTO, SessionGoalSnapshotDTO, SessionGoalUpdateRequest } from "../../types/backend";
import { errorMessage } from "../../utils/errorMessage";

interface GoalOptions {
  active?: boolean;
  mainThreadId?: string | null;
  /** 任务详情直接消费父列表的完整对象，不为同一详情重复读取和订阅。 */
  seed?: SessionGoalSnapshotDTO;
}

/** Goal 是持续任务的一种应用；所有入口共用读取、版本比较和控制回执处理。 */
export function useTaskGoal(port: number, workspaceId: string | null, sessionId: string | null,
  { active = true, mainThreadId, seed }: GoalOptions = {}) {
  const key = JSON.stringify([port, workspaceId, sessionId, mainThreadId]);
  const scope = useMemo(() => ({ released: false, snapshot: null as SessionGoalSnapshotDTO | null, streamFailed: false, readVersion: 0,
    reads: createSnapshotReadCoordinator(), writes: createSerialOperationQueue() }), [key]);
  const current = useRef(scope); current.current = scope;
  const enabled = useRef(active); enabled.current = active;
  const [view, setView] = useState({ scope, snapshot: null as SessionGoalSnapshotDTO | null,
    items: [] as PersistentTaskDTO[], tasksLoaded: false, reading: true, writing: false,
    error: null as string | null, streamError: null as string | null });
  const [connection, reconnect] = useState(0);
  const valid = useCallback(() => current.current === scope && !scope.released && enabled.current, [scope]);
  const apply = useCallback((incoming: SessionGoalSnapshotDTO) => {
    if (!valid() || incoming.sync_version < (scope.snapshot?.sync_version ?? 0)) return;
    parseSessionGoalSnapshot(incoming, sessionId!);
    if (mainThreadId) assertGoalOwner(incoming, { session_id: sessionId!, thread_id: mainThreadId });
    scope.snapshot = incoming;
    setView(previous => valid() ? { ...previous, scope, snapshot: incoming, reading: false } : previous);
  }, [scope, valid, sessionId, mainThreadId]);
  const accept = useCallback((snapshot: PersistentTaskSnapshot) => {
    if (!valid()) return;
    if (!snapshot.session_goal) throw new Error("会话持久任务快照缺少 Goal 槽位及清空版本");
    apply(snapshot.session_goal);
    setView(previous => ({ ...previous, items: snapshot.items, tasksLoaded: true, streamError: null }));
  }, [apply, valid]);
  const target = useMemo(() => {
    if (!workspaceId) return null;
    const owner = workspaceOwnerFromKey(workspaceId);
    return { gateway_id: owner.gatewayId, workspace_id: owner.workspaceId };
  }, [workspaceId]);

  const refresh = useCallback(async () => {
    if (!valid() || !target || !sessionId) return null;
    const readVersion = ++scope.readVersion;
    setView(previous => ({ ...previous, scope, reading: true, error: null }));
    try {
      const result = await scope.reads.read(key, signal => listPersistentTasks(port, target, signal, sessionId));
      if (!valid()) return null;
      accept(result);
      if (scope.streamFailed) { scope.streamFailed = false; reconnect(value => value + 1); }
      return scope.snapshot?.goal ?? null;
    } catch (cause) {
      if (cause instanceof SnapshotReadSupersededError || !valid()) return null;
      setView(previous => ({ ...previous, error: errorMessage(cause) }));
      throw cause;
    } finally {
      // 隐藏或被实时事件取代的读取也必须收尾，但不能结束后来发起的读取。
      if (!scope.released && scope.readVersion === readVersion) {
        setView(previous => previous.scope === scope ? { ...previous, reading: false } : previous);
      }
    }
  }, [accept, key, port, scope, sessionId, target, valid]);

  useEffect(() => {
    scope.released = false;
    setView({ scope, snapshot: null, items: [], tasksLoaded: false, reading: active && Boolean(sessionId), writing: false, error: null, streamError: null });
    return () => { scope.released = true; scope.reads.dispose(); };
  }, [scope]);
  useEffect(() => { if (seed && active) apply(seed); }, [seed, active, apply]);
  useEffect(() => {
    if (!active || !sessionId || !target || seed) return;
    let controller: AbortController | undefined;
    const connect = () => {
      controller?.abort();
      if (document.visibilityState === "hidden") { scope.reads.dispose(); return; }
      const reader = new AbortController(); controller = reader;
      scope.streamFailed = false;
      const publish = (value: PersistentTaskSnapshot) => {
        if (reader.signal.aborted) return;
        scope.reads.invalidate(key);
        accept(value);
      };
      void watchPersistentTasks(port, target, reader.signal, publish, event => {
        if (reader.signal.aborted) return;
        scope.reads.invalidate(key);
        setView(previous => ({ ...previous, items: event.task
          ? previous.items.some(item => item.task_id === event.task_id)
            ? previous.items.map(item => item.task_id === event.task_id ? event.task! : item)
            : [...previous.items, event.task]
          : previous.items.filter(item => item.task_id !== event.task_id) }));
        if (event.task?.goal?.session_id === sessionId) {
          const goal = parseSessionGoal(event.task.goal);
          apply({ session_id: sessionId, thread_id: goal.thread_id, goal,
            control_revision: goal.control_revision, sync_version: goal.sync_version });
        }
      }, sessionId).catch(cause => {
        if (!reader.signal.aborted && valid()) {
          scope.streamFailed = true;
          setView(previous => ({ ...previous, reading: false, streamError: errorMessage(cause) }));
        }
      });
    };
    connect();
    document.addEventListener("visibilitychange", connect);
    window.addEventListener("online", connect);
    return () => {
      controller?.abort();
      document.removeEventListener("visibilitychange", connect);
      window.removeEventListener("online", connect);
    };
  }, [port, target, sessionId, active, Boolean(seed), scope, key, connection, accept, apply, valid]);

  const mutate = useCallback((write: (expected: number, operationId: string) => Promise<SessionGoalOperationResultDTO>,
    expected?: number): Promise<SessionGoalOperationResultDTO> => scope.writes.run(key, async () => {
    if (!valid() || !workspaceId || !sessionId) throw new SnapshotReadSupersededError();
    // 尚未拿到初始化槽位时先确认版本，不能把“仍在加载”当成版本零。
    if (!scope.snapshot) await refresh();
    if (!valid() || !scope.snapshot) throw new SnapshotReadSupersededError();
    scope.reads.invalidate(key);
    setView(previous => ({ ...previous, reading: false, writing: true, error: null }));
    const operationId = crypto.randomUUID();
    try {
      let result: SessionGoalOperationResultDTO;
      try { result = await write(expected ?? scope.snapshot.control_revision, operationId); }
      catch (operationError) {
        let receipt: SessionGoalOperationResultDTO;
        try { receipt = await getSessionGoalOperation(port, sessionId, operationId, workspaceId); }
        catch (queryError) {
          throw new Error(`${errorMessage(operationError)}；查询 Goal 原操作回执失败：${errorMessage(queryError)}`);
        }
        if (receipt.status === "unknown") throw operationError;
        result = receipt;
      }
      if (result.status !== "applied" && result.status !== "already_missing") throw new Error(`Goal 操作未应用: ${result.status}`);
      scope.reads.invalidate(key);
      apply({ session_id: result.session_id, thread_id: result.thread_id, goal: result.goal,
        control_revision: result.control_revision, sync_version: result.sync_version });
      return result;
    } catch (cause) {
      let failure = cause;
      try { await refresh(); }
      catch (readError) { failure = new Error(`${errorMessage(cause)}；重新读取持续任务失败：${errorMessage(readError)}`); }
      if (!scope.released) setView(previous => previous.scope === scope ? { ...previous, error: errorMessage(failure) } : previous);
      throw failure;
    } finally {
      // 请求已发出后仍归原会话收尾，不受编辑器是否可见影响。
      if (!scope.released) setView(previous => previous.scope === scope ? { ...previous, writing: false } : previous);
    }
  }), [apply, key, port, refresh, scope, sessionId, valid, workspaceId]);
  const update = useCallback(async (payload: SessionGoalUpdateRequest) => {
    const frozen = { ...payload };
    const result = await mutate((expected, operationId) => updateSessionGoal(port, sessionId!, {
      ...frozen, operation_id: operationId, expected_control_revision: expected,
    }, workspaceId), frozen.expected_control_revision);
    if (!result.goal) throw new Error(`Goal 设置未返回已应用目标: ${result.status}`);
    return result.goal;
  }, [mutate, port, sessionId, workspaceId]);
  const clear = useCallback(async (expected?: number) => {
    await mutate((revision, operationId) => clearSessionGoal(port, sessionId!, {
      operation_id: operationId, expected_control_revision: revision,
    }, workspaceId), expected);
  }, [mutate, port, sessionId, workspaceId]);
  const snapshot = view.scope === scope ? view.snapshot : null;
  return { snapshot, goal: snapshot?.goal ?? null, controlRevision: snapshot?.control_revision ?? 0,
    tasks: view.scope === scope ? view.items : [], tasksLoaded: view.scope === scope && view.tasksLoaded,
    loading: view.scope !== scope || view.reading || view.writing, error: view.scope === scope ? view.error ?? view.streamError : null,
    refresh, update, clear };
}
