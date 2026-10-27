import type { TaskRuntimeStatusDTO } from "../../../api/persistentTasks";

export default function TaskRuntimeStatus({ runtime }: { runtime: TaskRuntimeStatusDTO | null }) {
  if (!runtime) return null;
  if (runtime.state === "failed") return <p role="alert">{runtime.error}；剩余 {runtime.recovering_tasks} 个任务尚未恢复完成。</p>;
  if (runtime.state === "running") return <p role="status">{runtime.recovering_tasks
    ? `工作区已就绪；${runtime.recovering_tasks} 个任务仍在后台恢复，其他任务独立推进。`
    : "持久任务运行时正常，后台恢复已完成。"}</p>;
  const label: Record<string, string> = { starting: "正在初始化", stopping: "正在停止", stopped: "已停止" };
  return <p role="status">持久任务运行时{label[runtime.state] ?? `状态未知：${runtime.state}`}；剩余 {runtime.recovering_tasks} 个任务尚未恢复完成。</p>;
}
