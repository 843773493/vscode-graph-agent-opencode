import { useMemo } from "react";
import { t } from "../../../../../shared/core/i18n/index.js";
import GoalManagerCard from "../../cards/GoalManagerCard";
import { useTaskGoal } from "../../../hooks/persistentTasks/useTaskGoal";
import type { PersistentTaskDTO } from "../../../api/persistentTasks";
import { parseSessionGoal } from "../../../protocol/sessionGoal";
import type { SessionGoalSnapshotDTO } from "../../../types/backend";

export default function TaskGoalDetail({ apiPort, workspaceId, sessionId, goal, snapshot }: {
  apiPort: number; workspaceId: string; sessionId: string; goal?: PersistentTaskDTO["goal"]; snapshot?: SessionGoalSnapshotDTO | null;
}) {
  const seed = useMemo(() => {
    if (snapshot) return snapshot;
    if (!goal) return undefined;
    const value = parseSessionGoal(goal);
    return { session_id: sessionId, thread_id: value.thread_id, goal: value,
      control_revision: value.control_revision, sync_version: value.sync_version };
  }, [goal, sessionId, snapshot]);
  const state = useTaskGoal(apiPort, workspaceId, sessionId, { seed });
  return <>
    <GoalManagerCard sessionId={sessionId} goal={state.goal} controlRevision={state.controlRevision}
      loading={state.loading} error={state.error}
      onRefresh={state.refresh} onUpdate={state.update} onClear={state.clear} />
    <p className="persistent-task-hint">{t("tasks.goal.hint")}</p>
  </>;
}
