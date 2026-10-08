import { useSyncExternalStore } from "react";

// 只管理本页观察订阅；Backend 成员、任务和路由状态仍由 Gateway 决定。
const pauses = new Map<string, number>();
const observers = new Map<string, Set<AbortController>>();
const listeners = new Set<() => void>();
interface ObservationState { paused: boolean; revision: number }
const ready: ObservationState = { paused: false, revision: 0 };
const states = new Map<string, ObservationState>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function notify(workspaceIds: readonly string[]): void {
  for (const workspaceId of workspaceIds) {
    states.set(workspaceId, {
      paused: pauses.has(workspaceId),
      revision: (states.get(workspaceId)?.revision ?? 0) + 1,
    });
  }
  for (const listener of listeners) listener();
}

export function useWorkspaceObservationState(workspaceId: string | null): ObservationState {
  return useSyncExternalStore(
    subscribe,
    () => workspaceId === null ? ready : states.get(workspaceId) ?? ready,
    () => ready,
  );
}

export function registerWorkspaceObservation(
  workspaceId: string | null,
  controller: AbortController,
): () => void {
  if (workspaceId === null) return () => {};
  if (pauses.has(workspaceId)) {
    controller.abort();
    return () => {};
  }
  const registered = observers.get(workspaceId) ?? new Set<AbortController>();
  observers.set(workspaceId, registered);
  registered.add(controller);
  return () => {
    registered.delete(controller);
    if (registered.size === 0) observers.delete(workspaceId);
  };
}

export function pauseWorkspaceObservations(workspaceIds: readonly string[]): () => void {
  const affected = [...new Set(workspaceIds)];
  for (const workspaceId of affected) {
    pauses.set(workspaceId, (pauses.get(workspaceId) ?? 0) + 1);
    for (const controller of observers.get(workspaceId) ?? []) controller.abort();
  }
  notify(affected);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    for (const workspaceId of affected) {
      const count = pauses.get(workspaceId)!;
      if (count === 1) pauses.delete(workspaceId);
      else pauses.set(workspaceId, count - 1);
    }
    notify(affected);
  };
}
