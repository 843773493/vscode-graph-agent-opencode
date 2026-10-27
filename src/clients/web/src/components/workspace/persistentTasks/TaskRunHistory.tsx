import { useEffect, useState } from "react";
import { inspectPersistentTaskRun, type PersistentTaskRunDTO, type PersistentTaskRunDetailDTO, type TaskThreadTargetDTO } from "../../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import { errorMessage } from "../../../utils/errorMessage";

const labels: Record<string, string> = {
  manual: "手动", interval: "固定间隔", cron: "Cron", external: "外部通知", execution_end: "工作执行结束",
  pending: "等待检查", checking: "正在判断", delivering: "正在投递", delivered: "已投递", checked: "检查已结束",
  failed: "失败", suppressed: "未投递（状态已变化）", skipped: "已跳过", wait: "条件未满足", proceed: "条件满足",
  complete: "已完成", needs_attention: "需要处理", completed: "已完成", completed_empty: "已完成（无输出）", cancelled: "已取消",
  unknown: "结果未知或仍在执行", interrupted: "已中断",
};
const label = (value: string) => labels[value] ?? value;
const time = (value: string | undefined) => value ? new Date(value).toLocaleString() : "—";

function RunDetail({ apiPort, target, run, onOpenThread }: {
  apiPort: number; target: GatewayWorkspaceRequestTarget; run: PersistentTaskRunDTO;
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
  }, [apiPort, target.workspace_id, target.backend_workspace_id, target.connection_kind, run.task_id, run.run_id, run.updated_at, version]);
  const open = async (thread: TaskThreadTargetDTO) => {
    setError(null);
    try { await onOpenThread(thread); }
    catch (cause) { setError(errorMessage(cause)); }
  };
  return <section aria-label={`运行详情 ${run.run_id}`}>
    <button onClick={() => setVersion((value) => value + 1)}>刷新运行详情</button>
    {error ? <div role="alert">{error}</div> : null}
    {!detail && !error ? <p role="status">正在读取冻结输入与执行证据…</p> : null}
    {detail ? <>
      <p>触发：{time(run.scheduled_for ?? run.created_at)} · {label(run.trigger_type)} · 定义版本 {run.definition_revision}</p>
      {detail.evaluation_target ? <p><button onClick={() => void open(detail.evaluation_target!)}>打开判断 Thread</button> <code>{detail.evaluation_target.uri}</code></p> : null}
      {detail.work_target ? <p><button onClick={() => void open(detail.work_target!)}>打开工作 Thread</button> <code>{detail.work_target.uri}</code></p> : null}
      {detail.consumption ? <p>已消费：输入已进入派发的模型请求。<br />模型调用：<code>{detail.consumption.model_call_id}</code><br />Assembly：<code>{detail.consumption.assembly_id}</code><br />请求 hash：<code>{detail.consumption.request_hash}</code></p>
        : <p>尚无输入进入派发请求的证据。</p>}
      {detail.execution_outcome ? <p>工作执行：{label(detail.execution_outcome)}</p> : null}
      {detail.action_content ? <details open><summary>实际投递正文（canonical）</summary><pre>{detail.action_content}</pre></details> : null}
      {run.decision ? <details><summary>结构化判断结果</summary><pre>{JSON.stringify(run.decision.result, null, 2)}</pre></details> : null}
      <details><summary>冻结判断输入</summary><pre>{JSON.stringify(detail.check_input, null, 2)}</pre></details>
      <details><summary>本次运行的任务定义</summary><pre>{JSON.stringify(detail.configuration, null, 2)}</pre></details>
    </> : null}
  </section>;
}

function RunRow({ apiPort, target, run, onOpenSession, onOpenThread }: {
  apiPort: number; target: GatewayWorkspaceRequestTarget; run: PersistentTaskRunDTO;
  onOpenSession: (sessionId: string) => Promise<void>; onOpenThread: (target: TaskThreadTargetDTO) => Promise<void>;
}) {
  const [expanded, setExpanded] = useState(false);
  return <li>
    <strong>{run.check_only ? "检查试运行" : label(run.trigger_type)} · {label(run.phase)}</strong>
    <div>{time(run.scheduled_for ?? run.created_at)} · 定义版本 {run.definition_revision}</div>
    {run.decision ? <p>{label(run.decision.decision)}：{run.decision.reason}</p> : null}
    {run.delivery ? <p>{run.delivery.state === "saved" ? "内容已保存，尚不代表模型已消费" : run.delivery.state === "accepted" ? "执行已接受，尚不代表执行已完成" : `未知投递状态：${run.delivery.state}`}<br />来源：<code>{run.delivery.source_id}</code>{run.delivery.execution_id ? <><br />执行：<code>{run.delivery.execution_id}</code></> : null}</p> : null}
    {run.evaluation ? <p>判断 Thread：<code>{run.evaluation.thread_uri}</code><br />判断执行：<code>{run.evaluation.execution_id}</code></p> : null}
    {run.generation ? <div><strong>会话动作：{run.generation.status}</strong>{run.generation.outputs.map((output) => <p key={output.session_id}><button onClick={() => void onOpenSession(output.session_id)}>{output.title || output.session_id}</button> · {output.navigation_path.join("/") || "工作区根"}</p>)}{run.generation.job_id ? <p>工作执行：<code>{run.generation.job_id}</code></p> : null}{run.generation.report_back_job_id ? <p>回报执行：<code>{run.generation.report_back_job_id}</code></p> : null}</div> : null}
    {run.error ? <div role="alert">{run.error}</div> : null}
    <button aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? "收起运行详情" : "查看运行详情"}</button>
    {expanded ? <RunDetail apiPort={apiPort} target={target} run={run} onOpenThread={onOpenThread} /> : null}
  </li>;
}

export default function TaskRunHistory(props: {
  apiPort: number; target: GatewayWorkspaceRequestTarget; runs: PersistentTaskRunDTO[];
  onOpenSession: (sessionId: string) => Promise<void>; onOpenThread: (target: TaskThreadTargetDTO) => Promise<void>;
}) {
  return <ol className="persistent-task-runs">{props.runs.map((run) => <RunRow key={run.run_id} {...props} run={run} />)}</ol>;
}
