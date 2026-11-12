import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
import { gatewayWorkspaceKey } from "../../../api/workspaceIdentity";
import { useState, type FormEvent } from "react";
import { getWorkspace } from "../../../api/workspace";
import { getSessionMainThread } from "../../../api/session/sessions";
import type { GatewayWorkspaceRequestTarget } from "../../../api/workspaceIdentity";
import GenerationActionEditor, { initialGeneration } from "./GenerationActionEditor";
import { errorMessage } from "../../../utils/errorMessage";
import type { PersistentTaskConfigurationDTO } from "../../../api/persistentTasks";

const triggers: Record<string, string> = { "builtin.manual": t("tasks.trigger.manual"), "builtin.interval": t("tasks.trigger.interval"), "builtin.cron": "Cron", "builtin.external": t("tasks.trigger.external"), "builtin.execution_end": t("tasks.trigger.executionEnd") };
const conditions: Record<string, string> = { "builtin.always": t("tasks.condition.always"), "builtin.script": t("tasks.type.script"), "builtin.model": t("tasks.type.model"), "builtin.rule": t("tasks.condition.rule") };
export type TaskTemplate = "scheduled" | "script" | "model" | "custom";

function conditionConfiguration(typeId: string): NonNullable<PersistentTaskConfigurationDTO["condition"]> {
  return {
    type_id: typeId, version: "1",
    ...(typeId === "builtin.script" ? { script: { command: [""], timeout_seconds: 30, output_limit_bytes: 65536, memory_limit_mb: 256 } } : {}),
    ...(typeId === "builtin.model" ? { model: { thread_uri: "", instruction: "" } } : {}),
    ...(typeId === "builtin.rule" ? { rule: { pointer: "", expected: true } } : {}),
  };
}

export function configurationProblem(config: PersistentTaskConfigurationDTO): string | null {
  const components = [config.trigger, config.condition, config.context, config.action];
  if (components.some((component) => !component || component.version !== "1")) return t("tasks.editor.unsupportedComponents");
  if (!config.trigger || !(config.trigger.type_id in triggers)) return t("tasks.editor.unsupportedTrigger", config.trigger?.type_id);
  if (!config.condition || !(config.condition.type_id in conditions)) return t("tasks.editor.unsupportedCondition", config.condition?.type_id);
  if (config.context?.type_id !== "builtin.literal" || !["builtin.runtime_source", "builtin.session_generation"].includes(config.action?.type_id ?? "")) return t("tasks.editor.unsupportedAction");
  const generation = config.action?.generation;
  if (config.action?.type_id === "builtin.session_generation" && (!generation || generation.generator_type?.type_id !== "builtin.agent_prompt" || generation.generator_type.version !== "1")) return t("tasks.editor.unsupportedGenerator");
  return null;
}

function initialConfiguration(template: TaskTemplate): PersistentTaskConfigurationDTO {
  return {
    name: "", trigger: template === "custom" ? { type_id: "builtin.manual", version: "1", timezone: "UTC" } : {
      type_id: "builtin.interval", version: "1", interval_seconds: template === "script" ? 10 : template === "model" ? 600 : 60, timezone: "UTC",
    },
    condition: conditionConfiguration(template === "script" ? "builtin.script" : template === "model" ? "builtin.model" : "builtin.always"),
    context: { type_id: "builtin.literal", version: "1", content: "", mode: "event" },
    action: { type_id: "builtin.runtime_source", version: "1", target_uri: "", delivery_mode: "context_and_run", delivery_policy: "after_turn" },
    policies: { completion: template === "script" ? "after_delivery" : "continuous", misfire: "skip", match_mode: "each_match" },
  };
}

export default function TaskEditor({ apiPort, target, configuration, initialTargetUri, template = "scheduled", busy, onSave, onCancel }: {
  initialTargetUri?: string;
  apiPort: number; target: GatewayWorkspaceRequestTarget; configuration?: PersistentTaskConfigurationDTO; template?: TaskTemplate; busy: boolean;
  onSave: (configuration: PersistentTaskConfigurationDTO) => Promise<boolean>; onCancel: () => void;
}) {
  const [draft, setDraft] = useState(() => {
    if (configuration) return configuration;
    const initial = initialConfiguration(template);
    return initialTargetUri ? { ...initial, action: { ...initial.action!, target_uri: initialTargetUri } } : initial;
  });
  const [ruleValue, setRuleValue] = useState(JSON.stringify(configuration?.condition?.rule ? configuration.condition.rule.expected : true));
  const [error, setError] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);
  const problem = configurationProblem(draft);
  busy = busy || resolving;
  const { trigger, condition, context, action, policies } = draft;
  if (problem || !trigger || !condition || !context || !action || !policies) return <div role="alert">{problem ?? t("tasks.editor.missingPolicies")}<button onClick={onCancel}>{t("tasks.editor.close")}</button></div>;

  const changeCondition = (typeId: string) => {
    if (typeId === "builtin.rule") setRuleValue("true");
    setDraft({ ...draft, condition: conditionConfiguration(typeId) });
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    let next = draft;
    if (condition.rule) {
      let expected: unknown;
      try { expected = JSON.parse(ruleValue); }
      catch { setError(t("tasks.editor.invalidRule")); return; }
      next = { ...draft, condition: { ...condition, rule: { ...condition.rule, expected } } };
    }
    setResolving(true);
    try {
      const generation = next.action?.generation;
      if (generation?.session_strategy && generation.context_source) {
        let strategy = generation.session_strategy;
        let source = generation.context_source;
        if (strategy.target && !strategy.target.thread_id) {
          const owner = await getSessionMainThread(apiPort, strategy.target.session_id, target);
          strategy = { ...strategy, target: { ...strategy.target, thread_id: owner.thread_id } };
        }
        if (source.kind === "live_session" && source.session_id && !source.thread_id) {
          const owner = await getSessionMainThread(apiPort, source.session_id, target);
          source = { ...source, thread_id: owner.thread_id };
        }
        next = { ...next, action: { ...next.action!, generation: { ...generation, session_strategy: strategy, context_source: source } } };
      }
      if (await onSave(next)) onCancel();
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setResolving(false); }
  };

  return <form className="persistent-task-editor" aria-label={configuration ? t("tasks.editor.edit") : t("tasks.editor.create")} onSubmit={(event) => void submit(event)}>
    <label>{t("tasks.editor.name")}<input aria-label={t("tasks.editor.name")} disabled={busy} required maxLength={200} value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
    <fieldset disabled={busy}><legend>{t("tasks.editor.when")}</legend>
      <label>{t("tasks.editor.trigger")}<select aria-label={t("tasks.editor.trigger")} value={trigger.type_id} onChange={(event) => {
        const type_id = event.target.value;
        setDraft({ ...draft, trigger: { type_id, version: "1", timezone: "UTC", ...(type_id === "builtin.interval" ? { interval_seconds: 60 } : {}), ...(type_id === "builtin.cron" ? { expression: "0 * * * *" } : {}), ...(type_id === "builtin.execution_end" ? { source_uri: "" } : {}) } });
      }}>{Object.entries(triggers).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      {trigger.type_id === "builtin.interval" ? <label>{t("tasks.editor.interval")}<input aria-label={t("tasks.editor.interval")} required type="number" min={1} value={trigger.interval_seconds} onChange={(event) => setDraft({ ...draft, trigger: { ...trigger, interval_seconds: Number(event.target.value) } })} /></label> : null}
      {trigger.type_id === "builtin.cron" ? <label>{t("tasks.editor.cron")}<input aria-label={t("tasks.editor.cron")} required value={trigger.expression} onChange={(event) => setDraft({ ...draft, trigger: { ...trigger, expression: event.target.value } })} /></label> : null}
      {["builtin.interval", "builtin.cron"].includes(trigger.type_id) ? <label>{t("tasks.editor.timezone")}<input aria-label={t("tasks.editor.timezone")} required value={trigger.timezone} onChange={(event) => setDraft({ ...draft, trigger: { ...trigger, timezone: event.target.value } })} /></label> : null}
      {trigger.type_id === "builtin.execution_end" ? <label>{t("tasks.editor.executionSource")}<input aria-label={t("tasks.editor.executionSource")} required value={trigger.source_uri} onChange={(event) => setDraft({ ...draft, trigger: { ...trigger, source_uri: event.target.value } })} /></label> : null}
    </fieldset>
    <fieldset disabled={busy}><legend>{t("tasks.editor.conditions")}</legend>
      <label><input aria-label={t("tasks.editor.enableConditions")} type="checkbox" checked={condition.type_id !== "builtin.always"} onChange={(event) => changeCondition(event.target.checked ? "builtin.script" : "builtin.always")} />{t("tasks.editor.enableConditions")}</label>
      {condition.type_id !== "builtin.always" ? <label>{t("tasks.editor.condition")}<select aria-label={t("tasks.editor.condition")} value={condition.type_id} onChange={(event) => changeCondition(event.target.value)}>{Object.entries(conditions).filter(([value]) => value !== "builtin.always").map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label> : null}
      {condition.script ? <>
        <p>{t("tasks.editor.scriptHint")}</p>
        {condition.script.command.map((argument, index) => <label key={index}>{index === 0 ? t("tasks.editor.program") : t("tasks.editor.argument", formatNumber(index))}<input aria-label={index === 0 ? t("tasks.editor.program") : t("tasks.editor.argument", formatNumber(index))} required={index === 0} value={argument} onChange={(event) => setDraft({ ...draft, condition: { ...condition, script: { ...condition.script!, command: condition.script!.command.map((value, position) => position === index ? event.target.value : value) } } })} />{index > 0 ? <button type="button" onClick={() => setDraft({ ...draft, condition: { ...condition, script: { ...condition.script!, command: condition.script!.command.filter((_, position) => position !== index) } } })}>{t("tasks.editor.deleteArgument", formatNumber(index))}</button> : null}</label>)}
        <button type="button" onClick={() => setDraft({ ...draft, condition: { ...condition, script: { ...condition.script!, command: [...condition.script!.command, ""] } } })}>{t("tasks.editor.addArgument")}</button>
        <label>{t("tasks.editor.timeout")}<input aria-label={t("tasks.editor.timeout")} type="number" required min={0.1} step="any" value={condition.script.timeout_seconds} onChange={(event) => setDraft({ ...draft, condition: { ...condition, script: { ...condition.script!, timeout_seconds: Number(event.target.value) } } })} /></label>
      </> : null}
      {condition.model ? <>
        <p>{t("tasks.editor.modelHint")}</p>
        <label>{t("tasks.editor.evaluationThread")}<input aria-label={t("tasks.editor.evaluationThread")} required value={condition.model.thread_uri} onChange={(event) => setDraft({ ...draft, condition: { ...condition, model: { ...condition.model!, thread_uri: event.target.value } } })} /></label>
        <label>{t("tasks.editor.instruction")}<textarea aria-label={t("tasks.editor.instruction")} required value={condition.model.instruction} onChange={(event) => setDraft({ ...draft, condition: { ...condition, model: { ...condition.model!, instruction: event.target.value } } })} /></label>
      </> : null}
      {condition.rule ? <>
        <label>{t("tasks.editor.pointer")}<input aria-label={t("tasks.editor.pointer")} value={condition.rule.pointer} onChange={(event) => setDraft({ ...draft, condition: { ...condition, rule: { ...condition.rule!, pointer: event.target.value } } })} /></label>
        <label>{t("tasks.editor.comparison")}<textarea aria-label={t("tasks.editor.comparison")} required value={ruleValue} onChange={(event) => setRuleValue(event.target.value)} /></label>
      </> : null}
    </fieldset>
    <fieldset disabled={busy}><legend>{t("tasks.editor.contentAndTarget")}</legend>
      <label>{t("tasks.editor.content")}<textarea aria-label={t("tasks.editor.content")} required={!action.generation} value={context.content} onChange={(event) => setDraft({ ...draft, context: { ...context, content: event.target.value } })} /></label>
      <label>{t("tasks.editor.workAction")}<select aria-label={t("tasks.editor.workAction")} value={action.type_id} onChange={(event) => {
        if (event.target.value === "builtin.runtime_source") {
          setDraft({ ...draft, action: { type_id: "builtin.runtime_source", version: "1", target_uri: initialTargetUri ?? "", delivery_mode: "context_and_run", delivery_policy: "after_turn" } });
        } else {
          setResolving(true); setError(null);
          void getWorkspace(apiPort, gatewayWorkspaceKey(target)).then((workspace) => {
            setDraft({ ...draft, context: { ...context, mode: "event" }, action: { type_id: "builtin.session_generation", version: "1", generation: initialGeneration(workspace.workspace_id) } });
          }).catch((cause: unknown) => setError(errorMessage(cause))).finally(() => setResolving(false));
        }
      }}><option value="builtin.runtime_source">{t("tasks.editor.publishToThread")}</option><option value="builtin.session_generation">{t("tasks.editor.generateSession")}</option></select></label>
      {action.generation ? <GenerationActionEditor apiPort={apiPort} target={target} name={draft.name} value={action.generation} onChange={(generation) => setDraft({ ...draft, action: { ...action, generation } })} /> : <>
      <label>{t("tasks.editor.workThread")}<input aria-label={t("tasks.editor.workThread")} required value={action.target_uri} onChange={(event) => setDraft({ ...draft, action: { ...action, target_uri: event.target.value } })} /></label>
      <label>{t("tasks.editor.action")}<select aria-label={t("tasks.editor.action")} value={action.delivery_mode} onChange={(event) => setDraft({ ...draft, action: { ...action, delivery_mode: event.target.value } })}><option value="context_and_run">{t("tasks.editor.publishAndRun")}</option><option value="context_only">{t("tasks.editor.publishOnly")}</option></select></label>
      </>}
      <label>{t("tasks.editor.completion")}<select aria-label={t("tasks.editor.completion")} value={policies.completion} onChange={(event) => setDraft({ ...draft, policies: { ...policies, completion: event.target.value } })}><option value="continuous">{t("tasks.editor.continuous")}</option><option value="after_delivery">{t("tasks.editor.afterDelivery")}</option></select></label>
    </fieldset>
    <details open={template === "custom" || undefined}><summary>{t("tasks.editor.advanced")}</summary><fieldset disabled={busy}>
      <label>{t("tasks.editor.matchMode")}<select aria-label={t("tasks.editor.matchMode")} value={policies.match_mode} onChange={(event) => setDraft({ ...draft, policies: { ...policies, match_mode: event.target.value } })}><option value="each_match">{t("tasks.editor.eachMatch")}</option><option value="first_match">{t("tasks.editor.firstMatch")}</option><option value="rising_edge">{t("tasks.editor.risingEdge")}</option></select></label>
      <label>{t("tasks.editor.misfire")}<select aria-label={t("tasks.editor.misfire")} value={policies.misfire} onChange={(event) => setDraft({ ...draft, policies: { ...policies, misfire: event.target.value } })}><option value="skip">{t("tasks.editor.skip")}</option><option value="run_latest">{t("tasks.editor.runLatest")}</option><option value="catch_up">{t("tasks.editor.catchUp")}</option></select></label>
      {!action.generation ? <>
      <label>{t("tasks.editor.deliveryPolicy")}<select aria-label={t("tasks.editor.deliveryPolicy")} value={action.delivery_policy} onChange={(event) => setDraft({ ...draft, action: { ...action, delivery_policy: event.target.value } })}><option value="after_turn">{t("tasks.editor.afterTurn")}</option><option value="after_tool_result">{t("tasks.editor.afterToolResult")}</option><option value="after_interrupt">{t("tasks.editor.afterInterrupt")}</option></select></label>
      <label>{t("tasks.editor.contentMode")}<select aria-label={t("tasks.editor.contentMode")} value={context.mode ?? "event"} onChange={(event) => setDraft({ ...draft, context: { ...context, mode: event.target.value } })}><option value="event">{t("tasks.editor.event")}</option><option value="state">{t("tasks.editor.sourceState")}</option></select></label>
      </> : null}
      {condition.script ? <>
        <label>{t("tasks.editor.memory")}<input aria-label={t("tasks.editor.memory")} required type="number" min={16} value={condition.script.memory_limit_mb} onChange={(event) => setDraft({ ...draft, condition: { ...condition, script: { ...condition.script!, memory_limit_mb: Number(event.target.value) } } })} /></label>
        <label>{t("tasks.editor.outputLimit")}<input aria-label={t("tasks.editor.outputLimit")} required type="number" min={1} value={condition.script.output_limit_bytes} onChange={(event) => setDraft({ ...draft, condition: { ...condition, script: { ...condition.script!, output_limit_bytes: Number(event.target.value) } } })} /></label>
      </> : null}
    </fieldset></details>
    {error ? <div role="alert">{error}</div> : null}
    <div className="persistent-task-actions"><button disabled={busy} type="submit">{t("tasks.action.update")}</button><button disabled={busy} type="button" onClick={onCancel}>{t("tasks.editor.cancel")}</button></div>
  </form>;
}
