import { useState } from "react";
import type { PersistentTaskDTO, PersistentTaskRunDTO } from "../../../api/persistentTasks";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import { usePersistentTasks } from "../../../hooks/persistentTasks/usePersistentTasks";
import { useWarmConfirm } from "../../shell/WarmConfirmProvider";
import TaskEditor, { configurationProblem } from "./TaskEditor";
import "../../../styles/workspace/persistentTasks.css";

const labels: Record<string, string> = {
  active: "已启用", paused: "已暂停", complete: "已完成", terminated: "已终止", needs_attention: "需要处理",
  waiting_trigger: "等待触发", recovering: "恢复中", pending: "等待检查", checking: "正在判断", delivering: "正在投递",
  delivered: "已投递", checked: "检查已结束", failed: "失败", suppressed: "未投递（状态已变化）", skipped: "已跳过",
  wait: "条件未满足", proceed: "条件满足", blocked: "已阻塞", budget_limited: "预算已用尽",
  manual: "手动", interval: "固定间隔", cron: "Cron", external: "外部通知", execution_end: "工作执行结束",
};
const label = (value: string) => labels[value] ?? value;
const time = (value: string | null | undefined) => value ? new Date(value).toLocaleString() : "—";
const deliveryLabels: Record<string, string> = { saved: "内容已保存，尚不代表模型已消费", accepted: "执行已接受，尚不代表执行已完成" };

function RunHistory({ runs }: { runs: PersistentTaskRunDTO[] }) {
  return <ol className="persistent-task-runs">{runs.map((run) => <li key={run.run_id}>
    <strong>{run.check_only ? "检查试运行" : label(run.trigger_type)} · {label(run.phase)}</strong>
    <div>{time(run.scheduled_for ?? run.created_at)} · 定义版本 {run.definition_revision}</div>
    {run.decision ? <p>{label(run.decision.decision)}：{run.decision.reason}</p> : null}
    {run.delivery ? <p>{deliveryLabels[run.delivery.state] ?? `未知投递状态：${run.delivery.state}`}<br />来源：<code>{run.delivery.source_id}</code>{run.delivery.execution_id ? <><br />执行：<code>{run.delivery.execution_id}</code></> : null}</p> : null}
    {run.evaluation ? <p>判断 Thread：<code>{run.evaluation.thread_uri}</code><br />判断执行：<code>{run.evaluation.execution_id}</code></p> : null}
    {run.error ? <div role="alert">{run.error}</div> : null}
    <details><summary>触发与判断记录</summary><pre>{JSON.stringify({ run_id: run.run_id, trigger_key: run.trigger_key, trigger_payload: run.trigger_payload, result: run.decision?.result, evaluation: run.evaluation, delivery: run.delivery, request_id: run.request_id }, null, 2)}</pre></details>
  </li>)}</ol>;
}

export default function PersistentTasks({ apiPort, target }: { apiPort: number; target: GatewayWorkspaceRequestTarget }) {
  const state = usePersistentTasks(apiPort, target);
  const confirm = useWarmConfirm();
  const [editor, setEditor] = useState<{ task: PersistentTaskDTO | null; key: string } | null>(null);
  const task = state.selected;
  const editable = task?.application_type === "automation" && task.configuration;
  const problem = editable ? configurationProblem(editable) : null;
  const terminal = task?.lifecycle === "terminated" || task?.lifecycle === "complete";
  const terminate = async (value: PersistentTaskDTO) => {
    if (await confirm({ title: "终止持久任务？", message: "将停止后续检查与投递。已经接受的工作执行继续由执行入口管理。", confirmText: "终止任务", danger: true })) await state.control(value, "terminate");
  };

  return <section className="persistent-tasks" aria-label="工作区持久任务">
    <header className="persistent-task-heading"><h2>持久任务</h2><button type="button" disabled={state.busy} onClick={() => setEditor({ task: null, key: crypto.randomUUID() })}>新建持久任务</button><button type="button" disabled={state.busy} onClick={() => void state.refresh()}>刷新任务</button></header>
    {state.error ? <div role="alert">{state.error}</div> : null}
    {!state.loaded && !state.error ? <p role="status">正在读取工作区任务…</p> : null}
    {state.loaded ? <p role="status">{state.items.filter((item) => item.recovering).length ? `${state.items.filter((item) => item.recovering).length} 个任务仍在恢复；其他任务独立推进。` : `当前工作区共 ${state.items.length} 个任务。`}</p> : null}
    <div className="persistent-task-layout">
      <div className="persistent-task-list" aria-label="持久任务列表">
        {state.loaded && state.items.length === 0 ? <p>还没有持久任务。</p> : null}
        {state.items.map((item) => <button type="button" key={item.task_id} aria-pressed={task?.task_id === item.task_id} onClick={() => { state.select(item.task_id); setEditor(null); }}>
          <strong>{item.configuration?.name ?? item.goal?.objective ?? item.task_id}</strong>
          <span>{item.application_type === "goal" ? "Goal" : "自动化"} · {label(item.lifecycle)} · {label(item.phase)}</span>
          {item.reason ? <span>{item.reason}</span> : null}
          <span>下次触发：{time(item.next_run_at)}</span>
          <small>{item.configuration?.action?.target_uri ?? item.goal?.session_id}</small>
        </button>)}
      </div>
      <div className="persistent-task-detail" aria-label="持久任务详情">
        {editor ? <TaskEditor key={editor.key} configuration={editor.task?.configuration ?? undefined} busy={state.busy}
          onCancel={() => setEditor(null)} onSave={(configuration) => editor.task ? state.update(editor.task, configuration) : state.create(configuration, editor.key)} /> : task ? <>
          <h3>{task.configuration?.name ?? task.goal?.objective}</h3>
          <p>{label(task.lifecycle)} · {label(task.phase)}{task.recovering ? " · 正在按持久进度恢复，工作区就绪不代表此任务已恢复完成。" : ""}</p>
          {task.reason ? <p>{task.reason}</p> : null}
          {editable ? <>
            <div className="persistent-task-actions">
              {task.lifecycle === "active" ? <button disabled={state.busy} onClick={() => void state.control(task, "pause")}>暂停任务</button> : !terminal ? <button disabled={state.busy} onClick={() => void state.control(task, "resume")}>恢复任务</button> : null}
              <button disabled={state.busy || terminal || Boolean(problem)} onClick={() => setEditor({ task, key: crypto.randomUUID() })}>编辑任务</button>
              <button disabled={state.busy || task.lifecycle !== "active"} onClick={() => void state.check(task, false)}>立即检查</button>
              <button disabled={state.busy || terminal} onClick={() => void state.check(task, true)}>检查试运行</button>
              <button disabled={state.busy || terminal} onClick={() => void terminate(task)}>终止任务</button>
            </div>
            {problem ? <div role="alert">{problem}</div> : null}
            <p>试运行会实际执行所属工作区的脚本或判断 Thread 的模型，仅检查条件，不执行后续工作动作。</p>
            <dl><dt>工作目标</dt><dd><code>{editable.action?.target_uri}</code></dd><dt>下次触发</dt><dd>{time(task.next_run_at)}</dd><dt>提示内容（当前定义）</dt><dd><pre>{editable.context?.content}</pre></dd></dl>
            <details><summary>完整任务定义</summary><pre>{JSON.stringify(editable, null, 2)}</pre></details>
            <h4>运行记录</h4>
            {state.history?.loading ? <p role="status">正在读取运行记录…</p> : state.history?.error ? <div role="alert">{state.history.error}</div> : state.history?.runs.length ? <RunHistory runs={state.history.runs} /> : <p>暂无运行记录。</p>}
          </> : task.goal ? <>
            <p>目标属于会话 <code>{task.goal.session_id}</code> 的主 Thread。目标与预算在该会话右侧的 Goal 标签管理。</p>
            <p>已用 token：{task.goal.tokens_used} · 已用时间：{task.goal.time_used_seconds} 秒</p>
          </> : <div role="alert">不支持的任务应用：{task.application_type} / {task.application_version}</div>}
        </> : <p>选择任务查看详情，或创建新的持久任务。</p>}
      </div>
    </div>
  </section>;
}
