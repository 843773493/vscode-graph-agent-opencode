import { failureSummary } from "../../../../../shared/core/failurePresentation";
import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
import { taskLabel as label } from "./taskPresentation";
import { formatLocalTimestamp } from "../../../utils/format";
import { useEffect, useState } from "react";
import { inspectPersistentTaskRun, type PersistentTaskRunSummaryDTO, type PersistentTaskRunDetailDTO, type TaskThreadTargetDTO } from "../../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import { errorMessage } from "../../../utils/errorMessage";

const time = (value: string | undefined) => value ? formatLocalTimestamp(value) : "—";

function RunDetail({ apiPort, target, run, onOpenThread }: {
  apiPort: number; target: GatewayWorkspaceRequestTarget; run: PersistentTaskRunSummaryDTO;
  onOpenThread: (target: TaskThreadTargetDTO) => Promise<void>;
}) {
  const [version, setVersion] = useState(0);
  const [detail, setDetail] = useState<PersistentTaskRunDetailDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setDetail(null); setError(null);
    void inspectPersistentTaskRun(apiPort, target, run.task_id, run.run_id, controller.signal).then((value) => {
      if (!controller.signal.aborted) setDetail(value);
    }, (cause: unknown) => { if (!controller.signal.aborted) setError(errorMessage(cause)); });
    return () => controller.abort();
  }, [apiPort, target.gateway_id, target.workspace_id, run.task_id, run.run_id, run.updated_at, version]);
  const open = async (thread: TaskThreadTargetDTO) => {
    setError(null);
    try { await onOpenThread(thread); }
    catch (cause) { setError(errorMessage(cause)); }
  };
  return <section aria-label={t("tasks.history.aria", run.run_id)}>
    <button onClick={() => setVersion((value) => value + 1)}>{t("tasks.history.refresh")}</button>
    {error ? <div className="failure-summary" role="alert">{failureSummary(error)}</div> : null}
    {!detail && !error ? <p role="status">{t("tasks.history.loadingDetail")}</p> : null}
    {detail ? <>
      <p>{t("tasks.history.triggered", time(run.scheduled_for ?? run.created_at), label(run.trigger_type), formatNumber(run.definition_revision))}</p>
      {detail.evaluation_target ? <p><button onClick={() => void open(detail.evaluation_target!)}>{t("tasks.history.openEvaluation")}</button> <code>{detail.evaluation_target.uri}</code></p> : null}
      {detail.work_target ? <p><button onClick={() => void open(detail.work_target!)}>{t("tasks.history.openWork")}</button> <code>{detail.work_target.uri}</code></p> : null}
      {detail.consumption ? <p>{t("tasks.history.consumed")}<br />{t("tasks.history.modelCall")}<code>{detail.consumption.model_call_id}</code><br />{t("tasks.history.assembly")}<code>{detail.consumption.assembly_id}</code><br />{t("tasks.history.hash")}<code>{detail.consumption.request_hash}</code></p>
        : <p>{t("tasks.history.notConsumed")}</p>}
      {detail.execution_outcome ? <p>{t("tasks.history.outcome", label(detail.execution_outcome))}</p> : null}
      {detail.action_content ? <details open><summary>{t("tasks.history.content")}</summary><pre>{detail.action_content}</pre></details> : null}
      {detail.run?.decision ? <details><summary>{t("tasks.history.decision")}</summary><pre>{JSON.stringify(detail.run.decision.result, null, 2)}</pre></details> : null}
      <details><summary>{t("tasks.history.input")}</summary><pre>{JSON.stringify(detail.check_input, null, 2)}</pre></details>
      <details><summary>{t("tasks.history.configuration")}</summary><pre>{JSON.stringify(detail.configuration, null, 2)}</pre></details>
    </> : null}
  </section>;
}

function RunRow({ apiPort, target, run, onOpenSession, onOpenThread }: {
  apiPort: number; target: GatewayWorkspaceRequestTarget; run: PersistentTaskRunSummaryDTO;
  onOpenSession: (sessionId: string) => Promise<void>; onOpenThread: (target: TaskThreadTargetDTO) => Promise<void>;
}) {
  const [expanded, setExpanded] = useState(false);
  return <li>
    <strong>{run.check_only ? t("tasks.action.checkOnly") : label(run.trigger_type)} · {label(run.phase)}</strong>
    <div>{t("tasks.history.revision", time(run.scheduled_for ?? run.created_at), formatNumber(run.definition_revision))}</div>
    {run.decision ? <p>{label(run.decision.decision)}：{run.decision.reason}</p> : null}
    {run.delivery ? <p>{run.delivery.state === "saved" ? t("tasks.history.saved") : run.delivery.state === "accepted" ? t("tasks.history.accepted") : t("tasks.history.unknownDelivery", run.delivery.state)}<br />{t("tasks.history.source")}<code>{run.delivery.source_id}</code>{run.delivery.execution_id ? <><br />{t("tasks.history.execution")}<code>{run.delivery.execution_id}</code></> : null}</p> : null}
    {run.evaluation ? <p>{t("tasks.history.evaluationThread")}<code>{run.evaluation.thread_uri}</code><br />{t("tasks.history.evaluationExecution")}<code>{run.evaluation.execution_id}</code></p> : null}
    {run.generation ? <div><strong>{t("tasks.history.sessionAction", label(run.generation.status))}</strong>{run.generation.outputs.map((output) => <p key={output.session_id}><button onClick={() => void onOpenSession(output.session_id)}>{output.title || output.session_id}</button> · {output.navigation_path.join("/") || t("tasks.generation.workspaceRoot")}</p>)}{run.generation.job_id ? <p>{t("tasks.history.workExecution")}<code>{run.generation.job_id}</code></p> : null}{run.generation.report_back_job_id ? <p>{t("tasks.history.reportExecution")}<code>{run.generation.report_back_job_id}</code></p> : null}</div> : null}
    {run.error ? <div className="failure-summary" role="alert">{failureSummary(run.error)}</div> : null}
    <button aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? t("tasks.history.collapse") : t("tasks.history.expand")}</button>
    {expanded ? <RunDetail apiPort={apiPort} target={target} run={run} onOpenThread={onOpenThread} /> : null}
  </li>;
}

export default function TaskRunHistory(props: {
  apiPort: number; target: GatewayWorkspaceRequestTarget; runs: PersistentTaskRunSummaryDTO[];
  onOpenSession: (sessionId: string) => Promise<void>; onOpenThread: (target: TaskThreadTargetDTO) => Promise<void>;
}) {
  return <ol className="persistent-task-runs">{props.runs.map((run) => <RunRow key={run.run_id} {...props} run={run} />)}</ol>;
}
