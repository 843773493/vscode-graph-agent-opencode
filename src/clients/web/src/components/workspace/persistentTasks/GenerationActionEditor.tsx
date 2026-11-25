import { failureSummary } from "../../../../../shared/core/failurePresentation";
import { t } from "../../../../../shared/core/i18n/index.js";
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
  if (!placement || !strategy || !source || !ui) return <div role="alert">{t("tasks.generation.missing")}</div>;
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
      if (!controller.signal.aborted) setPreview(`${result.title} · ${result.navigation_path.join("/") || t("tasks.generation.workspaceRoot")}`);
    } catch (cause) { if (!controller.signal.aborted) setError(errorMessage(cause)); }
    finally { if (!controller.signal.aborted) setPreviewing(false); }
  };
  return <>
    <label>{t("tasks.generation.prompt")}<textarea aria-label={t("tasks.generation.prompt")} required value={typeof value.config?.prompt === "string" ? value.config.prompt : ""} onChange={(event) => update({ config: { ...value.config, prompt: event.target.value } })} /></label>
    <label>{t("tasks.generation.strategy")}<select aria-label={t("tasks.generation.strategy")} value={strategy.mode} onChange={(event) => update({ session_strategy: {
      mode: event.target.value, concurrency: "queue", report_back: event.target.value === "fork_new_and_report_back" ? "continue_agent" : "none",
      ...(event.target.value !== "new_per_run" ? { target: { workspace_id: placement.workspace_id, session_id: "", thread_id: "" } } : {}),
    } })}><option value="new_per_run">{t("tasks.generation.new")}</option><option value="continue_existing">{t("tasks.generation.continue")}</option><option value="fork_new_and_report_back">{t("tasks.generation.forkReport")}</option></select></label>
    {strategy.target ? <>
      <label>{t("tasks.generation.targetSession")}<input aria-label={t("tasks.generation.targetSession")} required value={strategy.target.session_id} onChange={(event) => update({ session_strategy: { ...strategy, target: { ...strategy.target!, session_id: event.target.value, thread_id: "" } } })} /></label>
      <label>{t("tasks.generation.targetThread")}<input aria-label={t("tasks.generation.targetThread")} value={strategy.target.thread_id} placeholder={t("tasks.generation.resolveBeforeSave")} onChange={(event) => update({ session_strategy: { ...strategy, target: { ...strategy.target!, thread_id: event.target.value } } })} /></label>
    </> : null}
    {strategy.mode === "fork_new_and_report_back" ? <label>{t("tasks.generation.report")}<select aria-label={t("tasks.generation.report")} value={strategy.report_back} onChange={(event) => update({ session_strategy: { ...strategy, report_back: event.target.value } })}>
      <option value="none">{t("tasks.generation.reportNone")}</option><option value="link">{t("tasks.generation.reportLink")}</option><option value="summary">{t("tasks.generation.reportSummary")}</option><option value="summary_and_link">{t("tasks.generation.reportSummaryLink")}</option><option value="full">{t("tasks.generation.reportFull")}</option><option value="continue_agent">{t("tasks.generation.reportContinue")}</option>
    </select></label> : null}
    <label>{t("tasks.generation.placement")}<select aria-label={t("tasks.generation.placement")} value={placement.kind} onChange={(event) => update({ placement: { workspace_id: placement.workspace_id, kind: event.target.value, ...(event.target.value === "session" ? { session_id: "" } : event.target.value === "session_folder" ? { folder_id: "" } : {}) } })}>
      <option value="workspace">{t("tasks.generation.workspaceRoot")}</option><option value="session">{t("tasks.generation.underSession")}</option><option value="session_folder">{t("tasks.generation.underFolder")}</option>
    </select></label>
    {placement.kind === "session" ? <label>{t("tasks.generation.placementSession")}<input aria-label={t("tasks.generation.placementSession")} required value={placement.session_id ?? ""} onChange={(event) => update({ placement: { ...placement, session_id: event.target.value } })} /></label> : null}
    {placement.kind === "session_folder" ? <label>{t("tasks.generation.placementFolder")}<input aria-label={t("tasks.generation.placementFolder")} required value={placement.folder_id ?? ""} onChange={(event) => update({ placement: { ...placement, folder_id: event.target.value } })} /></label> : null}
    <label>{t("tasks.generation.titleTemplate")}<input aria-label={t("tasks.generation.titleTemplate")} required value={value.title_template} onChange={(event) => update({ title_template: event.target.value })} /></label>
    <label>{t("tasks.generation.pathTemplate")}<input aria-label={t("tasks.generation.pathTemplate")} value={value.path_template.join("/")} onChange={(event) => update({ path_template: event.target.value.split("/") })} /></label>
    <button type="button" disabled={previewing} onClick={() => void previewName()}>{previewing ? t("tasks.generation.previewing") : t("tasks.generation.preview")}</button>
    {preview ? <p role="status">{preview}</p> : null}{error ? <div className="failure-summary" role="alert">{failureSummary(error)}</div> : null}
    <details><summary>{t("tasks.generation.options")}</summary>
      <label>{t("tasks.generation.contextSource")}<select aria-label={t("tasks.generation.contextSource")} value={source.kind} onChange={(event) => update({ context_source: event.target.value === "fresh" ? { kind: "fresh" } : { kind: event.target.value, workspace_id: placement.workspace_id, session_id: "", thread_id: "" } })}><option value="fresh">{t("tasks.generation.fresh")}</option><option value="live_session">{t("tasks.generation.liveSession")}</option></select></label>
      {source.kind === "live_session" ? <>
        <label>{t("tasks.generation.sourceSession")}<input aria-label={t("tasks.generation.sourceSession")} required value={source.session_id ?? ""} onChange={(event) => update({ context_source: { ...source, session_id: event.target.value, thread_id: "" } })} /></label>
        <label>{t("tasks.generation.sourceThread")}<input aria-label={t("tasks.generation.sourceThread")} value={source.thread_id ?? ""} placeholder={t("tasks.generation.resolveMain")} onChange={(event) => update({ context_source: { ...source, thread_id: event.target.value } })} /></label>
      </> : null}
      <label>{t("tasks.generation.onStarted")}<select aria-label={t("tasks.generation.onStarted")} value={ui.on_run_started} onChange={(event) => update({ ui_policy: { ...ui, on_run_started: event.target.value } })}><option value="stay">{t("tasks.generation.stay")}</option><option value="open_generated">{t("tasks.generation.open")}</option></select></label>
      <label>{t("tasks.generation.onCompleted")}<select aria-label={t("tasks.generation.onCompleted")} value={ui.on_run_completed} onChange={(event) => update({ ui_policy: { ...ui, on_run_completed: event.target.value } })}><option value="none">{t("tasks.generation.noAction")}</option><option value="notify">{t("tasks.generation.notify")}</option><option value="open_generated">{t("tasks.generation.open")}</option><option value="open_on_failure">{t("tasks.generation.openFailure")}</option></select></label>
      <label>{t("tasks.generation.mountMissing")}<select aria-label={t("tasks.generation.mountMissing")} value={value.mount_missing} onChange={(event) => update({ mount_missing: event.target.value })}><option value="pause">{t("tasks.generation.pause")}</option><option value="fail">{t("tasks.generation.fail")}</option></select></label>
      <label>{t("tasks.generation.onDelete")}<select aria-label={t("tasks.generation.onDelete")} value={value.delete_outputs} onChange={(event) => update({ delete_outputs: event.target.value })}><option value="keep">{t("tasks.generation.keep")}</option><option value="cascade">{t("tasks.generation.cascade")}</option></select></label>
    </details>
  </>;
}
