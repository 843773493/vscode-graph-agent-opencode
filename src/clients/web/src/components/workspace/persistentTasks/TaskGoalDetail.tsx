import GoalManagerCard from "../../cards/GoalManagerCard";
import { useTaskGoal } from "../../../hooks/persistentTasks/useTaskGoal";

export default function TaskGoalDetail({ apiPort, workspaceId, sessionId, revision }: {
  apiPort: number; workspaceId: string; sessionId: string; revision?: number;
}) {
  const state = useTaskGoal(apiPort, workspaceId, sessionId, revision);
  return <>
    <GoalManagerCard sessionId={sessionId} goal={state.goal} loading={state.loading} error={state.error}
      onRefresh={state.refresh} onUpdate={state.update} onClear={state.clear} />
    <p className="persistent-task-hint">Goal 在主线程本轮执行结束后判断是否续跑；暂停后保留目标和累计用量。</p>
  </>;
}
