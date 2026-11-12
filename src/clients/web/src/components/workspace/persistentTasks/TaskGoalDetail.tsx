import { t } from "../../../../../shared/core/i18n/index.js";
import GoalManagerCard from "../../cards/GoalManagerCard";
import { useTaskGoal } from "../../../hooks/persistentTasks/useTaskGoal";

export default function TaskGoalDetail({ apiPort, workspaceId, sessionId, revision }: {
  apiPort: number; workspaceId: string; sessionId: string; revision?: number;
}) {
  const state = useTaskGoal(apiPort, workspaceId, sessionId, revision);
  return <>
    <GoalManagerCard sessionId={sessionId} goal={state.goal} loading={state.loading} error={state.error}
      onRefresh={state.refresh} onUpdate={state.update} onClear={state.clear} />
    <p className="persistent-task-hint">{t("tasks.goal.hint")}</p>
  </>;
}
