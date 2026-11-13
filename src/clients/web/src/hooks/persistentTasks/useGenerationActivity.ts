import { t } from "../../../../shared/core/i18n/index.js";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { TaskObservation } from "../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../api/workspaceIdentity";
import { errorMessage } from "../../utils/errorMessage";

type Activity = { phase: string; sessionId?: string };
const isTerminal = (phase: string) => ["delivered", "failed", "skipped"].includes(phase);

/** 初始快照建立在途运行基线；后续只消费同一状态流中的单条运行摘要。 */
export function useGenerationActivity(port: number, target: GatewayWorkspaceRequestTarget, active: boolean,
  callbacks: { onStatusChange: (text: string) => void; onOpenSession: (sessionId: string) => Promise<void> }) {
  const scope = useMemo(() => ({ seen: new Map<string, Activity>(), active, controller: new AbortController() }),
    [port, target.gateway_id, target.workspace_id, active]);
  const current = useRef(scope); current.current = scope;
  const actions = useRef(callbacks); actions.current = callbacks;
  const [error, setError] = useState({ scope, text: null as string | null });
  useEffect(() => {
    scope.controller = new AbortController();
    return () => scope.controller.abort();
  }, [scope]);
  const observe = useCallback((event: TaskObservation) => {
    const valid = () => current.current === scope && scope.active && !scope.controller.signal.aborted;
    if (!valid()) return;
    if ("snapshot" in event) {
      scope.seen.clear();
      for (const run of event.snapshot.active_runs) scope.seen.set(run.run_id, { phase: run.phase, sessionId: run.generation?.outputs[0]?.session_id });
      setError({ scope, text: null });
      return;
    }
    const { task, run } = event.changed;
    const policy = task?.configuration?.action?.generation?.ui_policy;
    if (!run || !policy) return;
    const result = run.generation;
    const output = result?.outputs[0];
    const before = scope.seen.get(run.run_id);
    scope.seen.set(run.run_id, { phase: run.phase, sessionId: output?.session_id });
    if (run.check_only) return;
    const terminal = isTerminal(run.phase) && !isTerminal(before?.phase ?? "");
    const started = output && !before?.sessionId;
    const open = (started && policy.on_run_started === "open_generated") || (terminal && (policy.on_run_completed === "open_generated" || (policy.on_run_completed === "open_on_failure" && run.phase === "failed")));
    if (terminal && policy.on_run_completed === "notify") actions.current.onStatusChange(t("tasks.generation.notification", task!.configuration!.name, result?.error ?? run.error ?? (run.phase === "delivered" ? t("tasks.state.complete") : result?.status ?? run.phase)));
    if (open && output) void actions.current.onOpenSession(output.session_id).then(() => {
      if (valid()) setError({ scope, text: null });
    }, (cause: unknown) => {
      if (valid()) setError({ scope, text: t("tasks.generation.readFailed", errorMessage(cause)) });
    });
  }, [scope]);
  return { observe, error: error.scope === scope ? error.text : null };
}
