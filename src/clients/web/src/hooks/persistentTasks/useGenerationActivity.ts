import { useEffect, useMemo, useRef, useState } from "react";
import { listPersistentTaskRuns, type PersistentTaskDTO } from "../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { errorMessage } from "../../utils/errorMessage";

type Activity = { phase: string; sessionId?: string };
const isTerminal = (phase: string) => ["delivered", "failed", "skipped"].includes(phase);

/** 由同一任务状态流触发读取，不另建生成器轮询或业务状态。初次读取只建立展示基线。 */
export function useGenerationActivity(port: number, target: GatewayWorkspaceRequestTarget, items: PersistentTaskDTO[], loaded: boolean,
  callbacks: { onStatusChange: (text: string) => void; onOpenSession: (sessionId: string) => Promise<void> }) {
  const owner = useMemo(() => ({ ...target }), [target.workspace_id, target.backend_workspace_id, target.connection_kind]);
  const seen = useMemo(() => new Map<string, Map<string, Activity>>(), [port, owner]);
  const actions = useRef(callbacks); actions.current = callbacks;
  const [error, setError] = useState({ seen, text: null as string | null });
  useEffect(() => {
    const controller = new AbortController();
    if (!loaded) return () => controller.abort();
    void (async () => {
      try {
        for (const task of items) {
          const policy = task.configuration?.action?.generation?.ui_policy;
          if (!policy) continue;
          const runs = await listPersistentTaskRuns(port, owner, task.task_id, controller.signal);
          if (controller.signal.aborted) return;
          const previous = seen.get(task.task_id);
          const next = new Map<string, Activity>();
          for (const run of [...runs].reverse()) {
            const result = run.generation;
            const status = result?.status ?? run.phase;
            const output = result?.outputs[0];
            const before = previous?.get(run.run_id);
            next.set(run.run_id, { phase: run.phase, sessionId: output?.session_id });
            if (!previous || run.check_only) continue;
            const terminal = isTerminal(run.phase) && !isTerminal(before?.phase ?? "");
            const started = output && !before?.sessionId;
            const open = (started && policy.on_run_started === "open_generated") || (terminal && (policy.on_run_completed === "open_generated" || (policy.on_run_completed === "open_on_failure" && run.phase === "failed")));
            if (terminal && policy.on_run_completed === "notify") actions.current.onStatusChange(`${task.configuration!.name}：${result?.error ?? run.error ?? (run.phase === "delivered" ? "已完成" : status)}`);
            if (open && output) await actions.current.onOpenSession(output.session_id);
            if (controller.signal.aborted) return;
          }
          seen.set(task.task_id, next);
        }
        if (!controller.signal.aborted) setError((previous) => previous.seen === seen && previous.text === null ? previous : { seen, text: null });
      } catch (cause) {
        if (!controller.signal.aborted) {
          const text = `读取会话动作状态失败：${errorMessage(cause)}`;
          setError((previous) => previous.seen === seen && previous.text === text ? previous : { seen, text });
        }
      }
    })();
    return () => controller.abort();
  }, [port, owner, seen, items, loaded]);
  return error.seen === seen ? error.text : null;
}
