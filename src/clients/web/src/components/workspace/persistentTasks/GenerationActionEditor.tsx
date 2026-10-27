import { useEffect, useState } from "react";
import { previewSessionGeneration } from "../../../api/session/sessionGenerations";
import type { SessionGenerationPreviewRequest } from "../../../types/backend";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import type { SessionGenerationActionConfigDTO } from "../../../types/protocol_generated/boxteam/workspace/v2/public";
import { errorMessage } from "../../../utils/errorMessage";

type Generation = SessionGenerationActionConfigDTO;

export function initialGeneration(workspaceId: string): Generation {
  return {
    generator_type: { type_id: "builtin.agent_prompt", version: "1" }, config: { prompt: "" },
    placement: { kind: "workspace", workspace_id: workspaceId }, context_source: { kind: "fresh" },
    session_strategy: { mode: "new_per_run", concurrency: "queue", report_back: "none" },
    title_template: "{generator.name} {generated_at:yyyy-MM-dd HH-mm-ss}",
    path_template: ["{generator.name}", "{generated_at:yyyy-MM-dd}"],
    ui_policy: { on_run_started: "stay", on_run_completed: "notify" }, mount_missing: "pause", delete_outputs: "keep",
  };
}

export default function GenerationActionEditor({ apiPort, target, name, value, onChange }: {
  apiPort: number; target: GatewayWorkspaceRequestTarget; name: string; value: Generation; onChange: (value: Generation) => void;
}) {
  const [preview, setPreview] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  // 输入改变立即废弃旧预览，取消后不得显示上一组命名结果。
  useEffect(() => { setPreview(null); setError(null); setPreviewing(false); }, [value, name]);
  const [request, setRequest] = useState<AbortController | null>(null);
  useEffect(() => () => request?.abort(), [request, value, name]);
  const { placement, session_strategy: strategy, context_source: source, ui_policy: ui } = value;
  if (!placement || !strategy || !source || !ui) return <div role="alert">会话动作缺少必要配置</div>;
  const update = (patch: Partial<Generation>) => onChange({ ...value, ...patch });
  const previewName = async () => {
    request?.abort();
    const controller = new AbortController(); setRequest(controller); setPreviewing(true); setError(null);
    try {
      const result = await previewSessionGeneration(apiPort, target, {
        name, title_template: value.title_template, path_template: value.path_template,
        session_title: typeof value.config?.session_title === "string" ? value.config.session_title : name,
        placement_kind: placement.kind as SessionGenerationPreviewRequest["placement_kind"], placement_node_id: placement.session_id || placement.folder_id || undefined,
        session_strategy: strategy.mode as SessionGenerationPreviewRequest["session_strategy"], target_session_id: strategy.target?.session_id,
      }, controller.signal);
      if (!controller.signal.aborted) setPreview(`${result.title} · ${result.navigation_path.join("/") || "工作区根"}`);
    } catch (cause) { if (!controller.signal.aborted) setError(errorMessage(cause)); }
    finally { if (!controller.signal.aborted) setPreviewing(false); }
  };
  return <>
    <label>生成提示词<textarea aria-label="生成提示词" required value={typeof value.config?.prompt === "string" ? value.config.prompt : ""} onChange={(event) => update({ config: { ...value.config, prompt: event.target.value } })} /></label>
    <label>会话策略<select aria-label="会话策略" value={strategy.mode} onChange={(event) => update({ session_strategy: {
      mode: event.target.value, concurrency: "queue", report_back: event.target.value === "fork_new_and_report_back" ? "continue_agent" : "none",
      ...(event.target.value !== "new_per_run" ? { target: { workspace_id: placement.workspace_id, session_id: "", thread_id: "" } } : {}),
    } })}><option value="new_per_run">每次创建新会话</option><option value="continue_existing">继续指定会话</option><option value="fork_new_and_report_back">分支后回报旧会话</option></select></label>
    {strategy.target ? <>
      <label>目标会话 ID<input aria-label="目标会话 ID" required value={strategy.target.session_id} onChange={(event) => update({ session_strategy: { ...strategy, target: { ...strategy.target!, session_id: event.target.value, thread_id: "" } } })} /></label>
      <label>目标 Thread ID<input aria-label="目标 Thread ID" value={strategy.target.thread_id} placeholder="留空时保存前解析主 Thread" onChange={(event) => update({ session_strategy: { ...strategy, target: { ...strategy.target!, thread_id: event.target.value } } })} /></label>
    </> : null}
    {strategy.mode === "fork_new_and_report_back" ? <label>回报方式<select aria-label="回报方式" value={strategy.report_back} onChange={(event) => update({ session_strategy: { ...strategy, report_back: event.target.value } })}>
      <option value="none">不回报</option><option value="link">链接</option><option value="summary">摘要</option><option value="summary_and_link">摘要与链接</option><option value="full">完整结果</option><option value="continue_agent">回报并继续旧会话</option>
    </select></label> : null}
    <label>生成会话挂载位置<select aria-label="生成会话挂载位置" value={placement.kind} onChange={(event) => update({ placement: { workspace_id: placement.workspace_id, kind: event.target.value, ...(event.target.value === "session" ? { session_id: "" } : event.target.value === "session_folder" ? { folder_id: "" } : {}) } })}>
      <option value="workspace">工作区根</option><option value="session">会话下</option><option value="session_folder">会话文件夹下</option>
    </select></label>
    {placement.kind === "session" ? <label>挂载会话 ID<input aria-label="挂载会话 ID" required value={placement.session_id ?? ""} onChange={(event) => update({ placement: { ...placement, session_id: event.target.value } })} /></label> : null}
    {placement.kind === "session_folder" ? <label>挂载文件夹 ID<input aria-label="挂载文件夹 ID" required value={placement.folder_id ?? ""} onChange={(event) => update({ placement: { ...placement, folder_id: event.target.value } })} /></label> : null}
    <label>标题模板<input aria-label="标题模板" required value={value.title_template} onChange={(event) => update({ title_template: event.target.value })} /></label>
    <label>导航路径模板<input aria-label="导航路径模板" value={value.path_template.join("/")} onChange={(event) => update({ path_template: event.target.value.split("/") })} /></label>
    <button type="button" disabled={previewing} onClick={() => void previewName()}>{previewing ? "正在预览…" : "预览生成位置"}</button>
    {preview ? <p role="status">{preview}</p> : null}{error ? <div role="alert">{error}</div> : null}
    <details><summary>会话动作选项</summary>
      <label>上下文来源<select aria-label="上下文来源" value={source.kind} onChange={(event) => update({ context_source: event.target.value === "fresh" ? { kind: "fresh" } : { kind: event.target.value, workspace_id: placement.workspace_id, session_id: "", thread_id: "" } })}><option value="fresh">全新上下文</option><option value="live_session">指定会话当前上下文</option></select></label>
      {source.kind === "live_session" ? <>
        <label>来源会话 ID<input aria-label="来源会话 ID" required value={source.session_id ?? ""} onChange={(event) => update({ context_source: { ...source, session_id: event.target.value, thread_id: "" } })} /></label>
        <label>来源 Thread ID<input aria-label="来源 Thread ID" value={source.thread_id ?? ""} placeholder="留空时解析主 Thread" onChange={(event) => update({ context_source: { ...source, thread_id: event.target.value } })} /></label>
      </> : null}
      <label>运行开始时<select aria-label="运行开始时" value={ui.on_run_started} onChange={(event) => update({ ui_policy: { ...ui, on_run_started: event.target.value } })}><option value="stay">停留当前会话</option><option value="open_generated">打开生成会话</option></select></label>
      <label>运行完成时<select aria-label="运行完成时" value={ui.on_run_completed} onChange={(event) => update({ ui_policy: { ...ui, on_run_completed: event.target.value } })}><option value="none">无操作</option><option value="notify">通知</option><option value="open_generated">打开生成会话</option><option value="open_on_failure">失败时打开</option></select></label>
      <label>挂载目标缺失时<select aria-label="挂载目标缺失时" value={value.mount_missing} onChange={(event) => update({ mount_missing: event.target.value })}><option value="pause">暂停并提示</option><option value="fail">本次运行失败</option></select></label>
      <label>删除任务时<select aria-label="删除任务时" value={value.delete_outputs} onChange={(event) => update({ delete_outputs: event.target.value })}><option value="keep">保留生成会话</option><option value="cascade">同时删除生成会话</option></select></label>
    </details>
  </>;
}
