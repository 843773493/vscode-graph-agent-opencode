import { useEffect, useRef, useState } from "react";
import { listPersistentTaskRuns, type PersistentTaskDTO } from "../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { errorMessage } from "../../utils/errorMessage";

/** 由同一任务状态流触发读取，不另建生成器轮询或业务状态。初次读取只建立展示基线。 */
export function useGenerationActivity(port: number, target: GatewayWorkspaceRequestTarget, items: PersistentTaskDTO[], loaded: boolean,
  callbacks: { onStatusChange: (text: string) => void; onOpenSession: (sessionId: string) => Promise<void> }) {
  const seen = useRef(new Map<string, Map<string, string>>());
  const actions = useRef(callbacks); actions.current = callbacks;
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    if (!loaded) return () => controller.abort();
    void (async () => {
      try {
        for (const task of items) {
          const policy = task.configuration?.action?.generation?.ui_policy;
          if (!policy) continue;
          const runs = await listPersistentTaskRuns(port, target, task.task_id, controller.signal);
          if (controller.signal.aborted) return;
          const previous = seen.current.get(task.task_id);
          const next = new Map<string, string>();
          for (const run of [...runs].reverse()) {
            const result = run.generation;
            const status = result?.status ?? run.phase;
            next.set(run.run_id, status);
            if (!previous || previous.get(run.run_id) === status || run.check_only) continue;
            const terminal = ["delivered", "failed", "skipped"].includes(run.phase);
            const started = result?.outputs.length && !["queued", "reporting", "completed", "failed"].includes(previous.get(run.run_id) ?? "");
            const open = (started && policy.on_run_started === "open_generated") || (terminal && (policy.on_run_completed === "open_generated" || (policy.on_run_completed === "open_on_failure" && run.phase === "failed")));
            if (terminal && policy.on_run_completed === "notify") actions.current.onStatusChange(`${task.configuration!.name}：${result?.error ?? run.error ?? (run.phase === "delivered" ? "已完成" : status)}`);
            const output = result?.outputs[0];
            if (open && output) await actions.current.onOpenSession(output.session_id);
            if (controller.signal.aborted) return;
          }
          seen.current.set(task.task_id, next);
        }
        if (!controller.signal.aborted) setError(null);
      } catch (cause) { if (!controller.signal.aborted) setError(`读取会话动作状态失败：${errorMessage(cause)}`); }
    })();
    return () => controller.abort();
  }, [port, target, items, loaded]);
  return error;
}
