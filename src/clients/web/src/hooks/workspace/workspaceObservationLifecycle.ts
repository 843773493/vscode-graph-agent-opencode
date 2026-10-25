import { useCallback, useSyncExternalStore } from "react";

// 只管理本页观察订阅；Backend 成员、任务和路由状态仍由 Gateway 决定。
const pauseDepthByWorkspaceId = new Map<string, number>();
const controllersByWorkspaceId = new Map<string, Set<AbortController>>();
const listenersByWorkspaceId = new Map<string, Set<() => void>>();
// revision 让同一同步批次内的暂停与恢复仍产生新快照，重建已中止的观察流。
interface ObservationState { paused: boolean; revision: number }
const defaultObservationState: ObservationState = { paused: false, revision: 0 };
const observationStateByWorkspaceId = new Map<string, ObservationState>();

function subscribeWorkspaceObservation(
  workspaceId: string | null,
  listener: () => void,
): () => void {
  if (workspaceId === null) return () => {};
  const workspaceListeners = listenersByWorkspaceId.get(workspaceId) ?? new Set<() => void>();
  listenersByWorkspaceId.set(workspaceId, workspaceListeners);
  workspaceListeners.add(listener);
  return () => {
    workspaceListeners.delete(listener);
    if (workspaceListeners.size > 0) return;
    listenersByWorkspaceId.delete(workspaceId);
    if (!pauseDepthByWorkspaceId.has(workspaceId)) {
      observationStateByWorkspaceId.delete(workspaceId);
    }
  };
}

function readWorkspaceObservationState(workspaceId: string | null): ObservationState {
  return workspaceId === null
    ? defaultObservationState
    : observationStateByWorkspaceId.get(workspaceId) ?? defaultObservationState;
}

function readServerObservationState(): ObservationState {
  return defaultObservationState;
}

function notifyWorkspace(workspaceId: string): void {
  const paused = pauseDepthByWorkspaceId.has(workspaceId);
  const previousState = observationStateByWorkspaceId.get(workspaceId) ?? defaultObservationState;
  // 重叠暂停只改变计数，不改变观察状态；避免无效通知触发消费者重新运行 effect。
  if (previousState.paused === paused) return;

  const nextState = { paused, revision: previousState.revision + 1 };
  const workspaceListeners = listenersByWorkspaceId.get(workspaceId);
  if (!paused && !workspaceListeners) observationStateByWorkspaceId.delete(workspaceId);
  else observationStateByWorkspaceId.set(workspaceId, nextState);
  for (const listener of workspaceListeners ?? []) listener();
}

function notify(workspaceIds: readonly string[]): void {
  for (const workspaceId of workspaceIds) notifyWorkspace(workspaceId);
}

export function useWorkspaceObservationState(workspaceId: string | null): ObservationState {
  const subscribe = useCallback(
    (listener: () => void) => subscribeWorkspaceObservation(workspaceId, listener),
    [workspaceId],
  );
  const getSnapshot = useCallback(
    () => readWorkspaceObservationState(workspaceId),
    [workspaceId],
  );
  return useSyncExternalStore(
    subscribe,
    getSnapshot,
    readServerObservationState,
  );
}

export function registerWorkspaceObservation(
  workspaceId: string | null,
  controller: AbortController,
): () => void {
  if (workspaceId === null) return () => {};
  if (pauseDepthByWorkspaceId.has(workspaceId)) {
    controller.abort();
    return () => {};
  }
  const workspaceControllers = controllersByWorkspaceId.get(workspaceId) ?? new Set<AbortController>();
  controllersByWorkspaceId.set(workspaceId, workspaceControllers);
  workspaceControllers.add(controller);
  return () => {
    workspaceControllers.delete(controller);
    if (workspaceControllers.size === 0) controllersByWorkspaceId.delete(workspaceId);
  };
}

export function pauseWorkspaceObservations(workspaceIds: readonly string[]): () => void {
  const affectedWorkspaceIds = [...new Set(workspaceIds)];
  for (const workspaceId of affectedWorkspaceIds) {
    pauseDepthByWorkspaceId.set(workspaceId, (pauseDepthByWorkspaceId.get(workspaceId) ?? 0) + 1);
    for (const controller of controllersByWorkspaceId.get(workspaceId) ?? []) controller.abort();
  }
  notify(affectedWorkspaceIds);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    for (const workspaceId of affectedWorkspaceIds) {
      const pauseDepth = pauseDepthByWorkspaceId.get(workspaceId)!;
      if (pauseDepth === 1) pauseDepthByWorkspaceId.delete(workspaceId);
      else pauseDepthByWorkspaceId.set(workspaceId, pauseDepth - 1);
    }
    notify(affectedWorkspaceIds);
  };
}
