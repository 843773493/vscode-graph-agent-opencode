import { useCallback, useEffect } from "react";
import { SnapshotReadSupersededError } from "../../../../shared/core/syncState";
import type { SessionGoalUpdateRequest } from "../../types/backend";
import type { SetAppState } from "../contentViewLoaderTypes";
import { useTaskGoal } from "../persistentTasks/useTaskGoal";

interface GoalTarget { sessionId: string; threadId: string; workspaceId: string | null }

/** 会话只投影统一任务 hook 的 Goal；不再维护自己的读写状态机和轮询。 */
export function useSessionGoalController({ active = true, apiPort, currentSessionId, currentMainThreadId,
  currentWorkspaceId, setState }: {
  active?: boolean; apiPort: number; currentSessionId: string | null; currentMainThreadId: string | null;
  currentWorkspaceId: string | null; setState: SetAppState;
}) {
  const task = useTaskGoal(apiPort, currentWorkspaceId, currentSessionId, { active, mainThreadId: currentMainThreadId });
  useEffect(() => {
    setState(previous => {
      if (previous.currentSession?.session_id !== currentSessionId || previous.currentSessionWorkspaceId !== currentWorkspaceId) return previous;
      const snapshot = task.snapshot;
      if (snapshot && snapshot.sync_version < previous.currentGoalSyncVersion) return previous;
      return { ...previous, currentGoal: snapshot?.goal ?? null, currentGoalSessionId: currentSessionId,
        currentGoalControlRevision: snapshot?.control_revision ?? 0, currentGoalSyncVersion: snapshot?.sync_version ?? 0,
        goalLoading: task.loading, goalError: task.error };
    });
  }, [currentSessionId, currentWorkspaceId, task.snapshot, task.loading, task.error, setState]);
  const check = useCallback((target?: GoalTarget) => {
    if (!active || !currentSessionId || !currentMainThreadId || !currentWorkspaceId || target && (
      target.sessionId !== currentSessionId || target.threadId !== currentMainThreadId || target.workspaceId !== currentWorkspaceId)) {
      throw new SnapshotReadSupersededError();
    }
  }, [active, currentSessionId, currentMainThreadId, currentWorkspaceId]);
  const refreshGoal = useCallback((target?: GoalTarget) => { check(target); return task.refresh(); }, [check, task.refresh]);
  const updateGoal = useCallback((payload: SessionGoalUpdateRequest, target?: GoalTarget) => {
    check(target); return task.update(payload);
  }, [check, task.update]);
  const clearGoal = useCallback((target?: GoalTarget, expectedControlRevision?: number) => {
    check(target); return task.clear(expectedControlRevision);
  }, [check, task.clear]);
  return { refreshGoal, updateGoal, clearGoal,
    persistentTasks: { items: task.tasks, loaded: task.tasksLoaded, error: task.error } };
}
